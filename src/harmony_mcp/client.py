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
from typing import Any, Literal

from aioharmony.const import ClientCallbackType, SendCommandDevice
from aioharmony.exceptions import HarmonyException
from aioharmony.harmonyapi import HarmonyAPI
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict

from .catalog import POWER_OFF_ID, Activity, Catalog, Command, norm
from .config import HubSettings, Protocol

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
        self, hub: HubSettings, protocol: Protocol | None = None, api_factory: ApiFactory = HarmonyAPI
    ) -> None:
        self.hub = hub
        self.protocol = protocol
        self._api_factory = api_factory
        self._api: Any = None
        self._task: asyncio.Task[None] | None = None
        self.available = False
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
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

        self._api = api
        self.catalog = Catalog.from_config(api.config)
        self.activity_id = api.current_activity[0]
        self.available = True
        log.info(
            "Connected to %s at %s: %d activities, %d devices",
            self.name,
            self.host,
            len(self.catalog.activities),
            len(self.catalog.devices),
        )

    # --- callbacks (called by aioharmony with one argument) -------------------
    def _on_connect(self, _ip: object) -> None:
        self.available = True

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

    async def start_activity(self, activity: Activity) -> bool:
        """Start an activity and wait until the hub says it's done.

        Returns False when it was already running (nothing sent): Harmony
        activities are states, so starting the current one is a no-op, which is
        what makes the tool idempotent.
        """
        api = self._require_idle(f"start {activity.name}")
        if self.activity_id == activity.activity_id:
            return False
        await self._run_activity(api, activity.activity_id, activity.name)
        return True

    async def power_off(self) -> bool:
        """Run the hub's power-off sequence. False when everything is already off."""
        api = self._require_idle("power off")
        if self.activity_id == POWER_OFF_ID:
            return False
        await self._run_activity(api, POWER_OFF_ID, "power off")
        return True

    async def _run_activity(self, api: Any, activity_id: int, what: str) -> None:
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

    async def send(self, command: Command, repeat: int = 1) -> None:
        """Press a command `repeat` times. The hub only replies when a press fails."""
        api = self._require_idle(f"send {command.name}")
        press = SendCommandDevice(device=command.device_id, command=command.name, delay=0)
        sequence: list[Any] = []
        for i in range(repeat):
            if i:
                sequence.append(self.repeat_gap)  # a bare float is a pause in aioharmony
            sequence.append(press)
        errors = await api.send_commands(sequence)
        if errors:
            e = errors[0]
            raise HarmonyError(
                f"{self.name} rejected {command.name} for {self.catalog.device_name(command.device_id)}: "
                f"{e.msg} (code {e.code})"
            )

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
