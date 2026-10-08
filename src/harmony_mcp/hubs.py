"""Several hubs behind one server: deciding which hub a tool call is about.

Every action tool takes an optional ``hub``. The rule is the same everywhere:
use it when given; otherwise look across all hubs, and act only if exactly one
hub matches. Two hubs both having a "Watch TV" activity, or activities running
on both TVs, is an *ambiguity*, and the answer is an error that names the hubs
so the model can ask or retry with ``hub``. Guessing would mean turning off the
wrong room's TV.

A hub that hasn't connected yet has an empty catalog, so its activities are
unknown. Errors say so rather than claiming the name doesn't exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from aioharmony.harmonyapi import HarmonyAPI

from .catalog import Activity, Command, Device
from .client import ApiFactory, HarmonyError, HubClient
from .config import Settings

T = TypeVar("T")

ALL = "all"


def _list(items: list[str]) -> str:
    return ", ".join(items) if items else "(none)"


class HubRegistry:
    def __init__(self, settings: Settings, api_factory: ApiFactory = HarmonyAPI) -> None:
        self.hubs = [HubClient(h, settings.protocol, api_factory) for h in settings.hubs]

    async def start(self) -> None:
        for h in self.hubs:
            await h.start()

    async def stop(self) -> None:
        await asyncio.gather(*(h.stop() for h in self.hubs))

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

    # --- lookups -----------------------------------------------------------------
    def activity(self, name: str, hub: str | None) -> tuple[HubClient, Activity]:
        return self._one(
            self.candidates(hub),
            lambda h: h.catalog.activity(name),
            "activity",
            name,
            lambda h: [a.name for a in h.catalog.activities],
        )

    def device(self, name: str, hub: str | None) -> tuple[HubClient, Device]:
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
