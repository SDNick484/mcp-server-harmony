"""A long-lived connection to the hub plus a cache of what it pushes.

The hub keeps one websocket open and pushes activity changes over it ("Watch
Shield is starting", "Watch Shield is now on"), the same pattern as the Shield's
remote protocol. So this class owns that connection for the life of the server,
keeps the latest state for ``get_status``, and holds the parsed config
(``Catalog``) that every tool validates against.

The first connect runs in a background task with backoff, so the server starts
(and answers ``get_status``) even when the hub is unreachable. After that,
aioharmony reconnects on its own and tells us through the connect/disconnect
callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from aioharmony.const import ClientCallbackType, SendCommandDevice
from aioharmony.exceptions import HarmonyException
from aioharmony.harmonyapi import HarmonyAPI
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict

from .catalog import POWER_OFF_ID, Activity, Catalog, Command, norm
from .config import HubSettings, Protocol
from .limits import (
    ACTIVITY_CAPACITY,
    ACTIVITY_RATE,
    MAX_CALL_SECONDS,
    MAX_DELAY_MS,
    MAX_HOLD_MS,
    MAX_REPEAT,
    MIN_DELAY_MS,
    PRESS_CAPACITY,
    PRESS_RATE,
    TokenBucket,
)

log = logging.getLogger(__name__)


class HarmonyError(ToolError):
    """A problem the model (and user) can act on.

    Only ToolError messages reach the model; any other exception shows up as
    a bare "Error executing tool", which would hide advice like "run check --host".
    """


# --- get_status's shape -----------------------------------------------------
# TypedDicts (from typing_extensions: Pydantic rejects typing.TypedDict on 3.11)
# make the SDK publish an outputSchema, so clients know the fields up front.
class ActivityRef(TypedDict):
    id: int
    name: str


class Status(TypedDict):
    hub: str
    host: str
    reachable: bool
    firmware: str | None
    power: Literal["on", "off"] | None
    current_activity: ActivityRef | None
    # Non-null while the hub runs an activity's start or power-off sequence;
    # commands are refused until it clears.
    transition: str | None


@dataclass(frozen=True)
class Outcome:
    """What a write did. ``sent`` describes each protocol frame (sent, or that would be in dry-run)."""

    changed: bool
    dry_run: bool = False
    sent: list[str] = field(default_factory=list)


# Builds the library's API object. Normally HarmonyAPI itself; tests pass a fake.
ApiFactory = Callable[..., Any]


class HubClient:
    """Owns the connection to one hub and the latest state it pushed.

    One per hub; HubRegistry (hubs.py) holds them all and decides which one a
    tool call is about.

    State (written by aioharmony's callbacks, read by tools):
      available         - connected right now.
      activity_id       - the running activity; -1 is "everything off"; None
                          until the hub has told us.
      starting_id       - set while an activity's start (or power-off) sequence
                          runs, cleared when the hub reports it finished.
      catalog           - the parsed config. Replaced when the hub reports a
                          config change (e.g. you edited an activity in the app).

    Everything runs on one asyncio loop, so callbacks and tools never overlap.
    """

    # An activity start walks every device in it (power on, set inputs, wait
    # for warm-up delays you configured in the app), so it can take a while.
    activity_timeout = 60.0
    # Gap between repeated presses. Without one, IR receivers often merge two
    # quick presses into one long press.
    repeat_gap = 0.4
    # First reconnect wait; doubles up to 60s. (Class attributes so tests can shrink them.)
    retry_delay = 1.0

    def __init__(
        self,
        hub: HubSettings,
        protocol: Protocol | None = None,
        api_factory: ApiFactory = HarmonyAPI,
        *,
        dry_run: bool = False,
    ) -> None:
        self.hub = hub
        self.protocol = protocol
        self.dry_run = dry_run
        self.press_bucket = TokenBucket(PRESS_CAPACITY, PRESS_RATE)
        self.activity_bucket = TokenBucket(ACTIVITY_CAPACITY, ACTIVITY_RATE)
        self._api_factory = api_factory
        self._api: Any = None
        self._task: asyncio.Task[None] | None = None
        self._resync_task: asyncio.Task[None] | None = None
        self.available = False
        # Set once the first connection attempt has finished, either way (HubRegistry.ready waits on it).
        self.tried = asyncio.Event()
        self.activity_id: int | None = None
        self.starting_id: int | None = None
        self.catalog = Catalog()

    # --- lifecycle -----------------------------------------------------------
    @property
    def host(self) -> str:
        return self.hub.host

    @property
    def name(self) -> str:
        """What to call this hub: the configured name, else the hub's own, else its address.

        aioharmony's ``name`` falls back to the IP when the hub hasn't said its
        friendlyName, so that case lands on the host too.
        """
        if self.hub.name:
            return self.hub.name
        live = self._api.name if self._api is not None else None
        return live if isinstance(live, str) and live else self.host

    def answers_to(self, wanted: str) -> bool:
        """True if `wanted` names this hub: configured name, the hub's own name, or its address."""
        key = norm(wanted)
        live = self._api.name if self._api is not None else None
        candidates = [self.hub.name, live if isinstance(live, str) else None, self.host]
        return any(c and norm(c) == key for c in candidates) or wanted.strip() == self.host

    async def start(self) -> None:
        self._task = asyncio.create_task(self._connect_forever(), name="harmony-connect")

    async def stop(self) -> None:
        for task in (self._task, self._resync_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._api is not None:
            with contextlib.suppress(Exception):
                await self._api.close()
        self.available = False

    async def _connect_forever(self) -> None:
        # Nothing awaits this task until shutdown, so log a crash instead of
        # letting it vanish.
        try:
            await self._connect()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Harmony connection task crashed")

    async def _connect(self) -> None:
        callbacks = ClientCallbackType(
            connect=self._on_connect,
            disconnect=self._on_disconnect,
            new_activity_starting=self._on_activity_starting,
            new_activity=self._on_activity,
            config_updated=self._on_config,
        )
        api = self._api_factory(ip_address=self.host, protocol=self.protocol, callbacks=callbacks)
        delay = self.retry_delay
        while True:
            try:
                if await api.connect():
                    break
                reason = "connect returned False"
            except (HarmonyException, OSError, TimeoutError) as exc:
                reason = str(exc) or type(exc).__name__
            log.info("Harmony hub %s unreachable (%s); retrying in %.0fs", self.name, reason, delay)
            self.tried.set()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

        self._api = api
        self.catalog = Catalog.from_config(api.config)
        self.activity_id = api.current_activity[0]
        self.available = True
        self.tried.set()
        log.info(
            "Connected to %s at %s: %d activities, %d devices",
            self.name,
            self.host,
            len(self.catalog.activities),
            len(self.catalog.devices),
        )

    # --- callbacks (called by aioharmony with one argument) -------------------
    def _on_connect(self, _ip: object) -> None:
        # aioharmony calls this on the first connect (before _connect has set
        # self._api) and again after each automatic reconnect.
        reconnected = self._api is not None and not self.available
        self.available = True
        if reconnected and (self._resync_task is None or self._resync_task.done()):
            self._resync_task = asyncio.ensure_future(self._resync())

    async def _resync(self) -> None:
        """Re-read config and current activity after a reconnect.

        While the socket was down the hub may have changed activity (someone
        used the remote), and a start that was in flight lost its replies, so
        the hub can be on an activity we reported as failed. aioharmony only
        reads state in connect(), not when its connector reconnects on its
        own, so we do it here.

        There's no public HarmonyAPI call for this; refresh_info_from_hub
        lives on the HarmonyClient behind HarmonyAPI._harmony_client.
        test_contract.py::test_recovers_from_a_malformed_frame pins it, so an
        aioharmony upgrade that moves it fails loudly instead of silently.
        """
        inner = getattr(self._api, "_harmony_client", None)
        if inner is None:
            return
        try:
            await inner.refresh_info_from_hub()
        except Exception as exc:  # the next reconnect or notification will try again
            log.warning("%s: couldn't refresh state after reconnecting: %s", self.name, exc)
            return
        self.catalog = Catalog.from_config(self._api.config)
        self.activity_id = self._api.current_activity[0]
        self.starting_id = None
        log.info("%s: state refreshed after reconnect", self.name)

    def _on_disconnect(self, _ip: object) -> None:
        self.available = False

    def _on_activity_starting(self, activity: tuple[int | None, str | None]) -> None:
        self.starting_id = activity[0]

    def _on_activity(self, activity: tuple[int | None, str | None]) -> None:
        self.activity_id = activity[0]
        self.starting_id = None

    def _on_config(self, config: dict[str, Any]) -> None:
        self.catalog = Catalog.from_config(config)
        log.info("%s: config changed; reloaded %d activities", self.name, len(self.catalog.activities))

    # --- commands ------------------------------------------------------------
    def _require(self) -> Any:
        if self._api is None or not self.available:
            raise HarmonyError(
                f"Can't reach the Harmony hub {self.name!r} at {self.host}. It may be offline or its IP may have "
                "changed; the server keeps retrying in the background."
            )
        return self._api

    def _require_idle(self, doing: str) -> Any:
        """Refuse to interleave with an activity sequence.

        While the hub walks through a start or power-off it is sending its own
        IR commands with delays between them; a command dropped into the middle
        can land on a device that is still warming up, or switch an input the
        sequence then switches back. Better to say "wait" than to half-work.
        """
        api = self._require()
        if self.starting_id is not None:
            what = "powering off" if self.starting_id == POWER_OFF_ID else f"starting {self._name(self.starting_id)}"
            raise HarmonyError(f"{self.name} is busy {what}; {doing} once it finishes (check get_status).")
        return api

    def _limit(self, bucket: TokenBucket, n: int, what: str) -> None:
        wait = bucket.take(n)
        if wait:
            when = "it's more than the limit allows at once" if wait == float("inf") else f"try again in {wait:.0f}s"
            raise HarmonyError(
                f"Rate limit on {self.name}: refusing to {what} ({when}). This guards against runaway loops; "
                "if this is deliberate, wait and retry."
            )

    async def start_activity(self, activity: Activity) -> Outcome:
        """Start an activity and wait until the hub says it's done.

        Unchanged (nothing sent) when it was already running: Harmony
        activities are states, so starting the current one is a no-op, which is
        what makes the tool idempotent.
        """
        api = self._require_idle(f"start {activity.name}")
        if self.activity_id == activity.activity_id:
            return Outcome(changed=False)
        self._limit(self.activity_bucket, 1, f"start {activity.name}")
        return await self._run_activity(api, activity.activity_id, activity.name)

    async def power_off(self) -> Outcome:
        """Run the hub's power-off sequence. Unchanged when everything is already off."""
        api = self._require_idle("power off")
        if self.activity_id == POWER_OFF_ID:
            return Outcome(changed=False)
        self._limit(self.activity_bucket, 1, "power off")
        return await self._run_activity(api, POWER_OFF_ID, "power off")

    async def _run_activity(self, api: Any, activity_id: int, what: str) -> Outcome:
        # The frame aioharmony sends for this (harmonyclient.start_activity).
        sent = [f"runactivity activityId={activity_id} ({what})"]
        if self.dry_run:
            log.info("[dry-run] %s: would send %s", self.name, sent[0])
            return Outcome(changed=False, dry_run=True, sent=sent)
        try:
            async with asyncio.timeout(self.activity_timeout):
                ok, msg = await api.start_activity(activity_id)
        except TimeoutError as exc:
            raise HarmonyError(
                f"{self.name} didn't finish '{what}' within {self.activity_timeout:.0f}s. It may still be running; "
                "check get_status before retrying."
            ) from exc
        if not ok:
            raise HarmonyError(f"{self.name} refused '{what}': {msg or 'no reason given'}")
        # The pushed "new activity" normally arrives before start_activity
        # returns, but don't depend on callback ordering for our own state.
        self.activity_id = activity_id
        self.starting_id = None
        return Outcome(changed=True, sent=sent)

    async def send(self, command: Command, repeat: int = 1, hold_ms: int = 0, delay_ms: int | None = None) -> Outcome:
        """Press a command `repeat` times. The hub only replies when a press fails (ASSUMPTION H-PRESS-SILENT).

        hold_ms: how long each press is held before release. aioharmony's
            SendCommandDevice.delay is exactly that: it sends press, sleeps,
            sends release (ASSUMPTION H-HOLD for what the hub does meanwhile).
        delay_ms: pause between repeats (default repeat_gap). A bare float in
            aioharmony's command list is a pause.
        """
        api = self._require_idle(f"send {command.name}")
        gap_ms = round(self.repeat_gap * 1000) if delay_ms is None else delay_ms
        if not 0 <= hold_ms <= MAX_HOLD_MS:
            raise HarmonyError(f"hold_ms must be 0-{MAX_HOLD_MS}.")
        # Only a caller's delay_ms is range-checked; the default is ours to choose.
        if delay_ms is not None and not MIN_DELAY_MS <= delay_ms <= MAX_DELAY_MS:
            raise HarmonyError(f"delay_ms must be {MIN_DELAY_MS}-{MAX_DELAY_MS}.")
        if not 1 <= repeat <= MAX_REPEAT:
            raise HarmonyError(f"repeat must be 1-{MAX_REPEAT}.")
        total = (repeat * hold_ms + (repeat - 1) * gap_ms) / 1000
        if total > MAX_CALL_SECONDS:
            raise HarmonyError(
                f"That would take {total:.1f}s ({repeat} x {hold_ms} ms held, {gap_ms} ms apart); the limit is "
                f"{MAX_CALL_SECONDS:.0f}s per call. Use fewer repeats or a shorter hold."
            )
        self._limit(self.press_bucket, repeat, f"press {command.name} x{repeat}")
        device = self.catalog.device_name(command.device_id)
        held = f", held {hold_ms} ms" if hold_ms else ""
        sent = [f"holdAction press+release {device}/{command.name}{held}"] * repeat
        if self.dry_run:
            log.info("[dry-run] %s: would send %s x%d", self.name, sent[0], repeat)
            return Outcome(changed=False, dry_run=True, sent=sent)
        press = SendCommandDevice(device=command.device_id, command=command.name, delay=hold_ms / 1000)
        sequence: list[Any] = []
        for i in range(repeat):
            if i:
                sequence.append(gap_ms / 1000)
            sequence.append(press)
        errors = await api.send_commands(sequence)
        if errors:
            e = errors[0]
            raise HarmonyError(f"{self.name} rejected {command.name} for {device}: {e.msg} (code {e.code})")
        return Outcome(changed=True, sent=sent)

    # --- reporting -----------------------------------------------------------
    def _name(self, activity_id: int | None) -> str:
        a = self.catalog.activity_by_id(activity_id)
        return a.name if a else str(activity_id)

    def _ref(self, activity_id: int | None) -> ActivityRef | None:
        if activity_id is None or activity_id == POWER_OFF_ID:
            return None
        return {"id": activity_id, "name": self._name(activity_id)}

    def current_activity(self) -> Activity | None:
        return self.catalog.activity_by_id(self.activity_id)

    def snapshot(self) -> Status:
        api = self._api
        power: Literal["on", "off"] | None = None
        if self.activity_id is not None:
            power = "off" if self.activity_id == POWER_OFF_ID else "on"
        return {
            "hub": self.name,
            "host": self.host,
            "reachable": self.available,
            "firmware": api.fw_version if api is not None else None,
            "power": power,
            "current_activity": self._ref(self.activity_id),
            "transition": (
                None
                if self.starting_id is None
                else "powering_off"
                if self.starting_id == POWER_OFF_ID
                else f"starting {self._name(self.starting_id)}"
            ),
        }
