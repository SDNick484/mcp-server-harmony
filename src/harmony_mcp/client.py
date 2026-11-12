"""A long-lived connection to the hub plus a cache of what it pushes.

The hub keeps one websocket open and pushes activity changes over it ("Watch
Shield is starting", "Watch Shield is now on"), the same pattern as the Shield's
remote protocol. So this class owns that connection for the life of the server,
keeps the latest state for ``get_status``, and holds the parsed config
(``Catalog``) that every tool validates against.

The first connect runs in a background task with backoff, so the server starts
(and answers ``get_status``) even when the hub is unreachable. That task is
supervised: if the connection cycle itself crashes (a bug, or something the
library raises that we don't expect), it's logged and restarted with backoff
rather than leaving the hub offline until the server restarts. After the first
connect, aioharmony reconnects on its own and tells us through the
connect/disconnect callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from aioharmony.const import ClientCallbackType, SendCommandDevice
from aioharmony.exceptions import HarmonyException
from aioharmony.harmonyapi import HarmonyAPI
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict

from .catalog import POWER_OFF_ID, Activity, Catalog, Command, norm
from .config import HubSettings, Protocol
from .history import History, Source
from .limits import (
    ACTIVITY_CAPACITY,
    ACTIVITY_RATE,
    DIGIT_GAP_MS,
    MAX_CALL_SECONDS,
    MAX_DELAY_MS,
    MAX_HOLD_MS,
    MAX_REPEAT,
    MAX_SEQUENCE,
    MIN_DELAY_MS,
    PRESS_CAPACITY,
    PRESS_RATE,
)
from .logsafe import mask_ip
from .ratelimit import TokenBucket, refusal
from .tasks import spawn_task, stop_task

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


class SleepTimerInfo(TypedDict):
    """A pending sleep timer. Lives in this server's memory only (see HubClient.set_sleep_timer)."""

    fires_at: str  # UTC, ISO 8601
    minutes_left: int  # rounded up, so "1" means under a minute; 0 while it is retrying past its time
    # Null while it is waiting. Non-null once it fired but the hub couldn't take the power off yet
    # (busy, unreachable, rate limited): why, and that it keeps trying for a bounded time.
    retrying: str | None


class SleepTimerEnd(TypedDict):
    """How the last sleep timer ended. Nothing can tell the model when a timer fires, so this is where it looks."""

    outcome: Literal["fired", "cancelled", "failed"]
    detail: str
    at: str  # UTC, ISO 8601


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
    sleep_timer: SleepTimerInfo | None
    last_sleep_timer: SleepTimerEnd | None


@dataclass
class _SleepTimer:
    """One hub's pending timer. ``due`` is on the event loop's monotonic clock; ``fires_at`` is only for display."""

    due: float
    fires_at: datetime
    task: asyncio.Task[None] | None = None
    # True from the moment it is due. The timer's own power off makes the hub report "off", and that must not
    # read as "someone else turned it off" (which drops the timer), so the activity callbacks ignore it then.
    firing: bool = False
    retry_reason: str | None = None


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
    # Gap between the different buttons of one send_sequence (the digits of a channel).
    sequence_gap = DIGIT_GAP_MS / 1000
    # First reconnect wait; doubles up to 60s. (Class attributes so tests can shrink them.)
    retry_delay = 1.0
    # How long stop() gives each background task. Under HubRegistry's per-hub STOP_TIMEOUT (5s), so a
    # task that won't stop is abandoned here, with a warning naming it, and there's still time to close
    # the connection. The registry's timeout stays as the backstop.
    stop_timeout = 3.0
    # Sleep timer: seconds per "minute" (tests shrink it so a 1-minute timer takes milliseconds), how long
    # after it is due it keeps trying while the hub is busy or unreachable, and the gap between tries.
    sleep_unit = 60.0
    sleep_retry_window = 300.0
    sleep_retry_delay = 15.0

    def __init__(
        self,
        hub: HubSettings,
        protocol: Protocol | None = None,
        api_factory: ApiFactory = HarmonyAPI,
        *,
        dry_run: bool = False,
        history: History | None = None,
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
        # Connection health, for diagnostics; deliberately not in Status/snapshot() (the model doesn't need
        # them, and get_status's outputSchema would change). Monotonic, so a wall-clock jump can't skew it.
        self.last_connected_at: float | None = None
        # Reconnects aioharmony reported, plus restarts after the connection task crashed.
        self.reconnect_count = 0
        # Bumped by each _run_activity. A forced power off can overtake a start that is still waiting for its
        # sequence; when that start returns, the counter tells it a later run owns the state now.
        self._runs = 0
        # The pending sleep timer and how the previous one ended. In memory on purpose: a TV that turns itself
        # off after a restart, long after anyone remembers asking, is worse than a timer that is lost.
        self._timer: _SleepTimer | None = None
        self.sleep_last: SleepTimerEnd | None = None
        # Activity history (history.py). Every change of activity_id goes through _set_activity, so the
        # pushed callback and our own bookkeeping after a start can't record the same change twice.
        self.history = history
        self._baselined = False  # the first state read since startup is a baseline, not a change
        # The activity a start or power off *we* sent is heading for, while it waits for the hub. A pushed
        # change to that activity during the wait is ours; any other push is from elsewhere. Set rather
        # than compared afterwards, so it doesn't matter whether the push or the reply arrives first.
        self._own_target: int | None = None
        # >0 while state is being re-read (after a reconnect, or by ping): a change found then happened
        # at an unknown time, so its record says it was noticed late.
        self._noticing = 0

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
        # stop_task, not cancel-and-await. A task can lose a cancel (anything it awaits can, on Python
        # 3.11), and awaiting it then never returns. Cancelling the await doesn't help: it only passes
        # another cancel to the task and goes on waiting, so HubRegistry's timeout couldn't bound it
        # either. And suppressing CancelledError around the await also swallowed the cancel that
        # timeout relies on. Both tasks at once, so two wedged ones cost one stop_timeout, not two.
        timer, self._timer = self._timer, None  # shutdown ends it without a record: nobody can read one
        await asyncio.gather(
            stop_task(timer.task if timer else None, self.stop_timeout, f"{self.name}'s sleep timer"),
            stop_task(self._resync_task, self.stop_timeout, f"{self.name}'s state refresh"),
            stop_task(self._task, self.stop_timeout, f"{self.name}'s connection"),
        )
        if self._api is not None:
            with contextlib.suppress(Exception):
                await self._api.close()
        self.available = False

    async def _connect_forever(self) -> None:
        """Run the connection cycle, restarting it if it crashes.

        Nothing awaits this task until shutdown, so an exception escaping _connect() used to end it
        silently: one log line, then the hub stayed offline until the server restarted. Now an
        unexpected exception is logged and the whole cycle (build the API, connect with backoff) starts
        over, with its own backoff so a bug that crashes every time can't spin. Expected failures
        (hub unreachable) never get here; _connect() retries those itself. CancelledError is shutdown
        (stop()), so it always propagates.
        """
        delay = self.retry_delay
        while True:
            try:
                await self._connect()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: connection task crashed; restarting in %.0fs", self.name, delay)
                # Don't make HubRegistry.ready() wait out its grace for a hub whose first try blew up.
                self.tried.set()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                self.reconnect_count += 1

    async def _connect(self) -> None:
        callbacks = ClientCallbackType(
            connect=self._on_connect,
            disconnect=self._on_disconnect,
            new_activity_starting=self._on_activity_starting,
            new_activity=self._on_activity,
            config_updated=self._on_config,
        )
        api = self._api_factory(ip_address=self.host, protocol=self.protocol, callbacks=callbacks)
        try:
            await self._establish(api)
        except Exception:
            # The supervisor will build a fresh API. Close this one first, or a half-set-up connection
            # (aioharmony's own reconnect loop included) would live on beside the new one, and both would
            # fire our callbacks.
            await self._discard(api)
            raise

    async def _establish(self, api: Any) -> None:
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
        # Normally the baseline. If the connection task crashed and restarted after that, whatever the
        # hub is running now was found by re-reading, not seen happening.
        self._set_activity(api.current_activity[0], late=True)
        self.available = True
        self.last_connected_at = time.monotonic()
        self.tried.set()
        log.info(
            "Connected to %s at %s: %d activities, %d devices",
            self.name,
            self.host,
            len(self.catalog.activities),
            len(self.catalog.devices),
        )

    async def _discard(self, api: Any) -> None:
        if self._api is api:
            self._api = None
        # _on_connect may already have marked us available during connect().
        self.available = False
        with contextlib.suppress(Exception):
            await api.close()

    async def ping(self, timeout: float = 3.0) -> bool:
        """Ask the hub for its state and say whether it answered within `timeout` seconds.

        `available` only says the socket hasn't reported a drop; a half-open TCP connection (the hub
        lost power, a switch rebooted) can look fine until something is sent. This sends something.
        The round trip is aioharmony's refresh_info_from_hub (config, hub info, current activity), the
        same private call _resync uses, so it costs a full config read: fine for an occasional probe,
        too heavy to run on every request. It also fires config_updated, which reloads the catalog.
        """
        if self._api is None or not self.available:
            return False
        try:
            async with asyncio.timeout(timeout):
                if (inner := getattr(self._api, "_harmony_client", None)) is not None:
                    with self._noticing_late():
                        await inner.refresh_info_from_hub()
                if self._api is None or self._api.current_activity is None:
                    return False
                return bool(self.available)
        except Exception:  # TimeoutError included; CancelledError isn't an Exception, so it propagates
            return False

    # --- callbacks (called by aioharmony with one argument) -------------------
    def _on_connect(self, _ip: object) -> None:
        # aioharmony calls this on the first connect (before _connect has set
        # self._api) and again after each automatic reconnect.
        reconnected = self._api is not None and not self.available
        self.available = True
        self.last_connected_at = time.monotonic()
        if reconnected:
            self.reconnect_count += 1
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
            with self._noticing_late():
                await inner.refresh_info_from_hub()
        except Exception as exc:  # the next reconnect or notification will try again
            log.warning("%s: couldn't refresh state after reconnecting: %s", self.name, exc)
            return
        self.catalog = Catalog.from_config(self._api.config)
        self._set_activity(self._api.current_activity[0], late=True)
        self.starting_id = None
        self._drop_timer_if_off()
        log.info("%s: state refreshed after reconnect", self.name)

    def _on_disconnect(self, _ip: object) -> None:
        self.available = False

    def _on_activity_starting(self, activity: tuple[int | None, str | None]) -> None:
        self.starting_id = activity[0]

    def _on_activity(self, activity: tuple[int | None, str | None]) -> None:
        # ASSUMPTION H-ACTIVITY-PUSH: a change made elsewhere arrives here unasked. ASSUMPTION H-OFF-PUSH: so does
        # a power off, as activity -1.
        # Ours if it is the activity a start/power off of ours is waiting on; otherwise someone else's
        # (the remote, the Harmony app, Home Assistant): the push doesn't say who asked.
        source: Source = "this_server" if activity[0] == self._own_target else "elsewhere"
        self._set_activity(activity[0], source=source)
        self.starting_id = None
        self._drop_timer_if_off()

    def _on_config(self, config: dict[str, Any]) -> None:
        self.catalog = Catalog.from_config(config)
        log.info("%s: config changed; reloaded %d activities", self.name, len(self.catalog.activities))

    # --- history -------------------------------------------------------------
    @contextlib.contextmanager
    def _noticing_late(self) -> Iterator[None]:
        self._noticing += 1
        try:
            yield
        finally:
            self._noticing -= 1

    def _set_activity(self, activity_id: int | None, *, source: Source = "elsewhere", late: bool = False) -> None:
        """The one place activity_id changes, so each change is recorded once.

        ``late``: this state came from re-reading, not from a push, so the change happened at an unknown time.
        """
        self.activity_id = activity_id
        if self.history is None or activity_id is None:
            return
        state = None if activity_id == POWER_OFF_ID else self._name(activity_id)
        if not self._baselined:
            self._baselined = True
            self.history.baseline(self.name, state)
        else:
            self.history.change(self.name, state, source, noticed_late=late or self._noticing > 0)

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
            raise HarmonyError(refusal(what, wait, device=self.name))

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

    async def power_off(self, force: bool = False) -> Outcome:
        """Run the hub's power-off sequence. Unchanged when everything is already off.

        force: send it regardless, the way you'd hold the remote's Off button when a start hangs. It skips
            the busy guard (an activity sequence still running, perhaps stuck waiting on a device) and the
            already-off shortcut (our state can be wrong after a hub hiccup), and sends runactivity -1
            straight away. The rate limit still applies. What the hub does with a power off in the middle
            of a sequence is ASSUMPTION H-FORCE-OFF.
        """
        if force:  # ASSUMPTION H-FORCE-OFF
            api = self._require()
        else:
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
        self._runs += 1
        run = self._runs
        self._own_target = activity_id
        try:
            async with asyncio.timeout(self.activity_timeout):
                ok, msg = await api.start_activity(activity_id)
        except TimeoutError as exc:
            raise HarmonyError(
                f"{self.name} didn't finish '{what}' within {self.activity_timeout:.0f}s. It may still be running; "
                "check get_status before retrying."
            ) from exc
        finally:
            # Only if no later run has taken the marker over (a forced power off overtaking this start).
            if run == self._runs:
                self._own_target = None
        if run != self._runs:
            # A later run (a forced power off) was sent while this one waited. Its state is the truth; ours
            # would put back an activity the hub has since turned off.
            raise HarmonyError(f"{self.name}: '{what}' was interrupted by a later power off; check get_status.")
        if not ok:
            raise HarmonyError(f"{self.name} refused '{what}': {msg or 'no reason given'}")
        # The pushed "new activity" normally arrives before start_activity
        # returns, but don't depend on callback ordering for our own state.
        self._set_activity(activity_id, source="this_server")
        self.starting_id = None
        self._drop_timer_if_off()
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

    # --- sleep timer ---------------------------------------------------------
    # A model acts only when it is called, so "turn the TV off in 45 minutes" needs something that outlives the
    # call: this server, which is long-lived and holds the hub connection. The timer is an asyncio task that
    # sleeps, then runs the same power_off() as the tool, so the busy guard, the rate limit and dry run all
    # apply unchanged. Switching activities keeps it (the person is still watching); the hub going off, by
    # anyone, drops it.
    def _end_timer(self, timer: _SleepTimer, outcome: Literal["fired", "cancelled", "failed"], detail: str) -> None:
        if self._timer is timer:  # a replaced timer's task must not clear its successor
            self._timer = None
        self.sleep_last = {"outcome": outcome, "detail": detail, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        log.info("%s: sleep timer %s: %s", self.name, outcome, detail)

    def _drop_timer_if_off(self) -> None:
        """The hub went off while a timer waited: nothing is left to turn off, so the timer goes too."""
        timer = self._timer
        if timer is None or timer.firing or self.activity_id != POWER_OFF_ID:
            return
        if timer.task is not None:
            timer.task.cancel()  # it is only sleeping here (firing is False), so a plain cancel is reliable
        self._end_timer(timer, "cancelled", "The hub was powered off before the timer fired, so it was dropped.")

    @property
    def is_on(self) -> bool:
        """On, or an activity is starting; powering off (or off) doesn't count."""
        if self.starting_id is not None:
            return self.starting_id != POWER_OFF_ID
        return self.activity_id is not None and self.activity_id != POWER_OFF_ID

    def has_sleep_timer(self) -> bool:
        return self._timer is not None

    async def set_sleep_timer(self, minutes: int) -> tuple[SleepTimerInfo, SleepTimerEnd | None]:
        """Schedule this hub's power off in `minutes`, replacing a pending timer.

        Returns the new timer and, when it replaced one, how that one was ended.
        """
        self._require()
        if not self.is_on:
            raise HarmonyError(f"{self.name} is off, so there is nothing to turn off later.")
        replaced = await self.cancel_sleep_timer("Replaced by a new timer.")
        loop = asyncio.get_running_loop()
        seconds = minutes * self.sleep_unit
        timer = _SleepTimer(due=loop.time() + seconds, fires_at=datetime.now(UTC) + timedelta(seconds=seconds))
        self._timer = timer
        timer.task = spawn_task(
            self._run_sleep_timer(timer),
            name=f"harmony-sleep-{self.name}",
            on_error=lambda exc: self._end_timer(timer, "failed", f"The timer crashed: {exc!r}"),
        )
        return self._timer_info(timer), replaced

    async def cancel_sleep_timer(self, why: str = "Cancelled.") -> SleepTimerEnd | None:
        """Stop a pending timer; None when there was none."""
        timer = self._timer
        if timer is None:
            return None
        await stop_task(timer.task, self.stop_timeout, f"{self.name}'s sleep timer")
        if self._timer is not timer:  # it finished (or was dropped) while we waited for it to stop
            return None
        self._end_timer(timer, "cancelled", why)
        return self.sleep_last

    async def _run_sleep_timer(self, timer: _SleepTimer) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0.0, timer.due - loop.time()))
        timer.firing = True
        deadline = loop.time() + self.sleep_retry_window
        while True:
            try:
                outcome = await self.power_off()
            except HarmonyError as exc:
                # Busy mid-sequence, unreachable, rate limited, refused by the hub: all can clear in a
                # moment, so keep trying for a bounded time and keep the last reason either way.
                timer.retry_reason = str(exc)
                if loop.time() + self.sleep_retry_delay > deadline:
                    self._end_timer(
                        timer,
                        "failed",
                        f"The TV was not turned off. Gave up retrying {self.sleep_retry_window / self.sleep_unit:g} "
                        f"minutes after the timer was due; last reason: {exc}",
                    )
                    return
                await asyncio.sleep(self.sleep_retry_delay)
                continue
            if outcome.dry_run:
                self._end_timer(timer, "fired", "DRY RUN: the power off was reported, not sent.")
            elif outcome.changed:
                self._end_timer(timer, "fired", "The hub was powered off.")
            else:
                # Off already: someone turned it off while we were retrying (the callbacks ignore a firing timer).
                self._end_timer(timer, "cancelled", "The hub was already off when the timer fired.")
            return

    def _timer_info(self, timer: _SleepTimer) -> SleepTimerInfo:
        left = timer.due - asyncio.get_running_loop().time()
        return {
            "fires_at": timer.fires_at.isoformat(timespec="seconds"),
            "minutes_left": max(0, math.ceil(left / self.sleep_unit)),
            "retrying": timer.retry_reason if timer.firing else None,
        }

    async def send_sequence(self, commands: Sequence[Command], what: str) -> Outcome:
        """Press different commands one after another, as one call: a channel number's digits.

        send() repeats one command; this sends a list of them with sequence_gap between presses. It is
        one operation to the guards, so a half-typed number can't happen:
          - one busy check up front (no press lands mid activity-sequence),
          - one rate-limit charge for all the presses, refused whole when the bucket is short (three
            of five digits would tune to the wrong channel, which is worse than none),
          - one dry-run report listing every press.
        `what` names it in the refusal and the log ("tune 702"). The caller resolved every command
        through the Catalog already; nothing here takes a name.
        """
        api = self._require_idle(what)
        if not 1 <= len(commands) <= MAX_SEQUENCE:
            raise HarmonyError(f"A sequence is 1-{MAX_SEQUENCE} presses, not {len(commands)}.")
        # Charge before the dry-run check, like send(): a dry run shows the limit a real call would hit.
        self._limit(self.press_bucket, len(commands), what)
        sent = [f"holdAction press+release {self.catalog.device_name(c.device_id)}/{c.name}" for c in commands]
        if self.dry_run:
            log.info("[dry-run] %s: would send %s", self.name, ", ".join(sent))
            return Outcome(changed=False, dry_run=True, sent=sent)
        sequence: list[Any] = []
        for i, c in enumerate(commands):
            if i:
                sequence.append(self.sequence_gap)
            sequence.append(SendCommandDevice(device=c.device_id, command=c.name, delay=0))
        errors = await api.send_commands(sequence)
        if errors:
            e = errors[0]
            # Presses before the failed one did go out; say so, so nobody assumes nothing happened.
            raise HarmonyError(
                f"{self.name} rejected {e.command.command} for {self.catalog.device_name(e.command.device)} while "
                f"{what}: {e.msg} (code {e.code}). The presses before it were sent."
            )
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
            "host": mask_ip(self.host),
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
            "sleep_timer": self._timer_info(self._timer) if self._timer is not None else None,
            "last_sleep_timer": self.sleep_last,
        }
