"""Several hubs behind one server: deciding which hub a tool call is about.

Every action tool takes an optional ``hub``. The rule is the same everywhere:
use it when given; otherwise look across all hubs, and act only if exactly one
hub matches. Two hubs both having a "Watch TV" activity, or activities running
on both TVs, is an *ambiguity*, and the answer is an error that names the hubs
so the model can ask or retry with ``hub``. Guessing would mean turning off the
wrong room's TV.

A hub that hasn't connected yet has an empty catalog, so its activities are
unknown. Errors say so rather than claiming the name doesn't exist.

Hub-qualified references: anywhere a tool takes an activity or device name, it
also takes "<hub>/<name>" ("Den/Listen to Music"), which is what the list
tools return as ``ref``. That lets the model copy one string instead of
pairing a name with a hub argument. If a name itself contains "/" and also
reads as a qualified reference, the call is refused as ambiguous rather than
guessed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TypeVar

import aioharmony.hubconnector_websocket as ws_connector
from aioharmony.harmonyapi import HarmonyAPI

from .catalog import Activity, Command, Device
from .client import ApiFactory, HarmonyError, HubClient
from .config import Settings

T = TypeVar("T")
log = logging.getLogger(__name__)

STOP_TIMEOUT = 5.0  # seconds per hub to close its connection at shutdown

ALL = "all"


def _list(items: list[str]) -> str:
    return ", ".join(items) if items else "(none)"


def ref(hub: HubClient, name: str) -> str:
    """The hub-qualified reference the list tools return, e.g. "Den/Watch TV"."""
    return f"{hub.name}/{name}"


class HubRegistry:
    def __init__(self, settings: Settings, api_factory: ApiFactory = HarmonyAPI) -> None:
        self.settings = settings
        self.hubs = [HubClient(h, settings.protocol, api_factory, dry_run=settings.dry_run) for h in settings.hubs]
        if settings.port is not None:
            # aioharmony has one module-level port for every hub (ASSUMPTION
            # H-PORT); only the simulator ever needs another one.
            ws_connector.DEFAULT_HUB_PORT = settings.port

    async def start(self) -> None:
        for problem in self.settings.problems:
            log.warning("Config: %s", problem)
        if self.settings.dry_run:
            log.warning("DRY RUN: hubs are read, but nothing that changes anything is sent.")
        for h in self.hubs:
            await h.start()

    async def stop(self) -> None:
        """Close every hub, giving each a bounded time so one wedged hub can't hang shutdown."""

        async def stop_one(h: HubClient) -> None:
            try:
                async with asyncio.timeout(STOP_TIMEOUT):
                    await h.stop()
            except TimeoutError:
                log.warning("%s: didn't close within %.0fs; abandoning it", h.name, STOP_TIMEOUT)

        with contextlib.suppress(Exception):
            await asyncio.gather(*(stop_one(h) for h in self.hubs))

    # --- choosing hubs -------------------------------------------------------------
    def names(self) -> str:
        return _list([h.name for h in self.hubs])

    def candidates(self, hub: str | None) -> list[HubClient]:
        """The hubs a call may be about: the named one, or all of them."""
        if not self.hubs:
            raise HarmonyError(
                "No Harmony hub configured. Run `mcp-server-harmony check --host <ip>` once per hub, "
                "or set HARMONY_HOSTS."
            )
        if hub is None:
            return list(self.hubs)
        match = [h for h in self.hubs if h.answers_to(hub)]
        if not match:
            raise HarmonyError(f"No hub named {hub!r}. Hubs: {self.names()}")
        return match[:1]

    def _offline_note(self, pool: list[HubClient]) -> str:
        offline = [h.name for h in pool if not h.available]
        return f" ({_list(offline)} not connected, so its names are unknown.)" if offline else ""

    def _one(
        self,
        pool: list[HubClient],
        find: Callable[[HubClient], T | None],
        kind: str,
        wanted: str,
        known: Callable[[HubClient], list[str]],
    ) -> tuple[HubClient, T]:
        hits = [(h, x) for h in pool if (x := find(h)) is not None]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise HarmonyError(
                f"{kind.capitalize()} {wanted!r} exists on {_list([h.name for h, _ in hits])}; pass hub to choose."
            )
        per_hub = "; ".join(f"{h.name}: {_list(known(h))}" for h in pool)
        raise HarmonyError(f"Unknown {kind} {wanted!r}. {per_hub}.{self._offline_note(pool)}")

    # --- hub-qualified references ------------------------------------------------
    def qualify(self, name: str, hub: str | None, exists: Callable[[HubClient, str], bool]) -> tuple[str, str | None]:
        """Split "Den/Watch TV" into ("Watch TV", "Den") when "Den" names a hub.

        `exists(hub, name)` says whether a hub has an item by that exact name;
        it decides the ambiguous case of a name that itself contains "/".
        """
        prefix, sep, rest = name.partition("/")
        if not sep or not rest.strip():
            return name, hub
        named = [h for h in self.hubs if h.answers_to(prefix)]
        if not named:
            return name, hub
        if any(exists(h, name) for h in self.hubs):
            raise HarmonyError(
                f"{name!r} could be the name itself or {rest.strip()!r} on {named[0].name}; "
                "pass the name and hub separately."
            )
        if hub is not None and not named[0].answers_to(hub):
            raise HarmonyError(f"{name!r} names hub {named[0].name}, but hub={hub!r} was also given.")
        return rest.strip(), prefix.strip()

    # --- lookups -----------------------------------------------------------------
    def activity(self, name: str, hub: str | None) -> tuple[HubClient, Activity]:
        name, hub = self.qualify(name, hub, lambda h, n: h.catalog.activity(n) is not None)
        return self._one(
            self.candidates(hub),
            lambda h: h.catalog.activity(name),
            "activity",
            name,
            lambda h: [a.name for a in h.catalog.activities],
        )

    def device(self, name: str, hub: str | None) -> tuple[HubClient, Device]:
        name, hub = self.qualify(name, hub, lambda h, n: h.catalog.device(n) is not None)
        return self._one(
            self.candidates(hub),
            lambda h: h.catalog.device(name),
            "device",
            name,
            lambda h: [d.name for d in h.catalog.devices],
        )

    def commands_for(self, target: str | None, hub: str | None) -> tuple[HubClient, str, tuple[Command, ...]]:
        """(hub, "activity X" or "device Y", its commands) for an activity or device name.

        None means the running activity. A name may be an activity on one hub
        and a device on another; that's ambiguous like any other repeat.
        """
        if target is None:
            running_hub, running = self.running(hub)
            return running_hub, f"activity {running.name}", running.commands
        target, hub = self.qualify(
            target, hub, lambda h, n: h.catalog.activity(n) is not None or h.catalog.device(n) is not None
        )
        pool = self.candidates(hub)
        hits: list[tuple[HubClient, str, tuple[Command, ...]]] = []
        for h in pool:
            if (a := h.catalog.activity(target)) is not None:
                hits.append((h, f"activity {a.name}", a.commands))
            if (d := h.catalog.device(target)) is not None:
                hits.append((h, f"device {d.name}", d.commands))
        if len(hits) == 1:
            return hits[0]
        if hits:
            where = _list([f"{h.name} ({what})" for h, what, _ in hits])
            raise HarmonyError(f"{target!r} matches {where}; pass hub to choose.")
        per_hub = "; ".join(
            f"{h.name}: {_list([a.name for a in h.catalog.activities] + [d.name for d in h.catalog.devices])}"
            for h in pool
        )
        raise HarmonyError(f"No activity or device named {target!r}. {per_hub}.{self._offline_note(pool)}")

    def running(self, hub: str | None) -> tuple[HubClient, Activity]:
        """The running activity, when exactly one of the candidate hubs has one."""
        pool = self.candidates(hub)
        on = [(h, a) for h in pool if (a := h.current_activity()) is not None]
        if len(on) == 1:
            return on[0]
        if on:
            running = _list([f"{h.name} ({a.name})" for h, a in on])
            raise HarmonyError(f"Activities are running on {running}; pass hub to choose.")
        where = pool[0].name if hub is not None else "any hub"
        raise HarmonyError(f"No activity is running on {where}. Name a device, or start an activity first.")

    def to_power_off(self, hub: str | None) -> list[HubClient]:
        """Which hubs power_off should act on. Empty means everything is already off.

        A named hub is returned as is: its own power_off says "already off", or
        why it can't be reached. With no hub, the one hub that is on; two or more
        on is ambiguous (they're different TVs). "all" means every hub that is on,
        plus any not connected, so the caller can report that it couldn't check them.
        """
        if hub is not None and hub.strip().lower() != ALL:
            return self.candidates(hub)
        pool = self.candidates(None)
        on = [h for h in pool if h.current_activity() is not None or h.starting_id is not None]
        if hub is None:
            if len(on) > 1:
                raise HarmonyError(
                    f"Several hubs are on ({_list([h.name for h in on])}); pass hub, or hub='all' for every one."
                )
            return on
        return [h for h in pool if h in on or not h.available]
