"""A record of what each hub's activity changed from and to, and when.

The hub pushes every activity change over the websocket we already hold open,
including ones made with the physical remote or the Harmony app. Until now the
server kept only the latest state; this keeps the changes, so a model can answer
"what was on at 9 last night?" or "did someone turn the bedroom TV on from the
remote?". It is the counterpart of ``sofabaton_get_recent_presses`` in the sibling
server, and the tool is shaped like it (newest first, ``after_seq`` to catch up).

Two kinds of record:

  change             the hub's running activity went from one state to another (off is a state).
  recording_started  the first state read after this server started. Not a change: it only says
                     "from here on we were watching". Over stdio the server lives only while the
                     MCP client does, so there are gaps where nothing was watching, and a reader
                     must be able to tell "nothing happened" from "we weren't running". The
                     answer to "what was on at 9pm?" can be "not recorded".

Where a change came from (``source``) is ``this_server`` when it is the result of a start or power
off this server sent, else ``elsewhere``: the physical remote, the Harmony app, or another client such
as Home Assistant. The hub's push doesn't say who asked, so those can't be told apart.

Persistence: events are kept in memory (bounded) and appended to a JSON Lines file in the config
directory, so history survives a restart. The file is capped (oldest dropped), created readable by the
owner only, and can be turned off (``HARMONY_HISTORY=off`` or ``"history": false`` in config.json). This
is a household's viewing habits on its own disk, so records hold hub and activity *names* only, never an
address. A write failure is logged once and never breaks a tool call or the connection.

The clock is injected so tests don't sleep.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from typing_extensions import TypedDict

from .limits import HISTORY_FILE_SLACK, HISTORY_MAX_EVENTS
from .logsafe import mask_ip

log = logging.getLogger(__name__)

HISTORY_FILE = "history.jsonl"

Kind = Literal["change", "recording_started"]
Source = Literal["this_server", "elsewhere"]


class HistoryEvent(TypedDict):
    seq: int  # one counter across all hubs; poll with after_seq
    at: str  # UTC, ISO 8601. For a noticed_late change: when it was noticed, not when it happened
    hub: str  # the hub's name at the time (never an address)
    kind: Kind
    from_activity: str | None  # null: everything was off (or, for recording_started, nothing is known before)
    to_activity: str | None  # null: everything is off
    # null for recording_started; else who asked. "elsewhere" is the remote, the Harmony app or another client.
    source: Source | None
    # True when the change was found by re-reading state after a reconnect (or a health check), so it
    # happened at an unknown time while the socket was down, somewhere between the previous event and `at`.
    noticed_late: bool
    # How long from_activity had been the state, in seconds. null when unknown (recording_started).
    previous_lasted_seconds: int | None
    # True when previous_lasted_seconds is only a minimum: the previous state began before recording
    # started (it follows a recording_started marker), so it may have lasted longer.
    previous_lasted_at_least: bool


def utcnow() -> datetime:
    return datetime.now(UTC)


def safe_name(name: str) -> str:
    """The hub's name for the record. A hub with no name is called by its address; don't store that."""
    return mask_ip(name)


class History:
    """The events, in memory and (optionally) on disk. One per server, shared by every hub."""

    def __init__(
        self,
        path: Path | None,
        *,
        enabled: bool = True,
        clock: Callable[[], datetime] = utcnow,
        max_events: int = HISTORY_MAX_EVENTS,
        file_slack: int = HISTORY_FILE_SLACK,
    ) -> None:
        self.enabled = enabled
        self.path = path if enabled else None
        self._clock = clock
        self.max_events = max_events
        self._file_slack = file_slack
        self._events: deque[HistoryEvent] = deque(maxlen=max_events)
        self._seq = 0
        self._file_lines = 0
        self._write_failed = False
        # When this server process began recording; tells a reader where the current session starts.
        self.session_started = _iso(clock())
        # Per hub: (activity name or None for off, when that state began, began-before-recording?)
        self._current: dict[str, tuple[str | None, datetime, bool]] = {}
        if self.path is not None:
            self._load()

    # --- recording ----------------------------------------------------------
    def baseline(self, hub: str, state: str | None) -> None:
        """The first state read for this hub since the server started: a marker, not a change."""
        if not self.enabled:
            return
        hub = safe_name(hub)
        now = self._clock()
        self._current[hub] = (state, now, True)
        self._add(
            {
                "seq": 0,
                "at": _iso(now),
                "hub": hub,
                "kind": "recording_started",
                "from_activity": None,
                "to_activity": state,
                "source": None,
                "noticed_late": False,
                "previous_lasted_seconds": None,
                "previous_lasted_at_least": False,
            }
        )

    def change(self, hub: str, state: str | None, source: Source, *, noticed_late: bool = False) -> bool:
        """Record a hub's move to `state`. Returns False (and records nothing) if it was already there.

        The caller decides *what* the state is; this decides whether it is news. That is the one place
        duplicates are dropped, so the pushed callback and a start's own bookkeeping can both report it.
        """
        if not self.enabled:
            return False
        hub = safe_name(hub)
        now = self._clock()
        if hub not in self._current:
            # A change before any baseline can't be placed in time; treat it as the baseline.
            self.baseline(hub, state)
            return False
        before, since, at_least = self._current[hub]
        if before == state:
            return False
        self._current[hub] = (state, now, False)
        self._add(
            {
                "seq": 0,
                "at": _iso(now),
                "hub": hub,
                "kind": "change",
                "from_activity": before,
                "to_activity": state,
                "source": source,
                "noticed_late": noticed_late,
                # A wall clock can step backwards (NTP); a negative duration would be nonsense.
                "previous_lasted_seconds": max(0, round((now - since).total_seconds())),
                "previous_lasted_at_least": at_least,
            }
        )
        return True

    def _add(self, event: HistoryEvent) -> None:
        self._seq += 1
        event["seq"] = self._seq
        self._events.append(event)
        self._append_to_file(event)

    # --- reading ------------------------------------------------------------
    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def recording_since(self) -> str | None:
        """When the oldest retained record was made (None before anything is recorded)."""
        return self._events[0]["at"] if self._events else None

    def page(self, hub: str | None, after_seq: int | None, limit: int) -> tuple[list[HistoryEvent], bool]:
        """(events, expired). Newest first, or oldest first after `after_seq` to catch up in order.

        `expired` means the caller's after_seq no longer lines up: records after it were dropped by the
        cap, or the seq is ahead of ours (history was off or deleted and the counter restarted).
        `hub` filters by name; last_seq stays global so polling one hub never skips another's events.
        """
        rows = [e for e in self._events if hub is None or e["hub"] == safe_name(hub)]
        oldest = self._events[0]["seq"] if self._events else self._seq + 1
        expired = after_seq is not None and (after_seq + 1 < oldest or after_seq > self._seq)
        if after_seq is None:
            return rows[-limit:][::-1], expired
        return [e for e in rows if e["seq"] > after_seq][:limit], expired

    # --- the file -----------------------------------------------------------
    def _load(self) -> None:
        assert self.path is not None
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self._fail(f"read {self.path}", exc)
            return
        bad = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            self._file_lines += 1
            try:
                event = _validated(json.loads(line))
            except (ValueError, KeyError, TypeError):
                bad += 1  # a torn last line after a crash, or a hand edit: skip it, keep the rest
                continue
            self._events.append(event)
            self._seq = max(self._seq, event["seq"])
        if bad:
            log.warning("History: skipped %d unreadable line(s) in %s", bad, self.path)

    def _append_to_file(self, event: HistoryEvent) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # O_CREAT with 0o600: owner only from the first byte, not chmod after the data is there.
            # A tiny blocking append per activity change (a few a day), so it stays on the event loop.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
            self._file_lines += 1
            # Compact only once the file is `slack` lines over the cap, so we rewrite it rarely, not on every append.
            if self._file_lines > self.max_events + self._file_slack:
                self._compact()
        except OSError as exc:
            self._fail(f"write {self.path}", exc)

    def _compact(self) -> None:
        """Rewrite the file with just the retained events (the oldest are dropped).

        Written beside it and renamed over it, so a crash mid-rewrite leaves the old file whole.
        """
        assert self.path is not None
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".history-", suffix=".tmp")  # mode 0o600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for e in self._events:
                    f.write(json.dumps(e, separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._file_lines = len(self._events)

    def _fail(self, what: str, exc: OSError) -> None:
        # Once, not per event: an unwritable directory would otherwise log on every change. History keeps
        # working in memory; only the restart-survival is lost.
        if not self._write_failed:
            self._write_failed = True
            log.warning("History: couldn't %s (%s); keeping history in memory only", what, exc)

    @property
    def persisting(self) -> bool:
        """True while events are going to the file (enabled, a path, and no write has failed)."""
        return self.path is not None and not self._write_failed


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _validated(raw: Any) -> HistoryEvent:
    """A loaded line as an event, or an exception: the file is outside our control, so check the shape."""
    if not isinstance(raw, dict):
        raise TypeError("not an object")
    if raw["kind"] not in ("change", "recording_started"):
        raise ValueError("kind")
    for key in ("seq", "at", "hub"):
        if not isinstance(raw[key], int if key == "seq" else str):
            raise TypeError(key)
    for key in ("from_activity", "to_activity"):
        if raw[key] is not None and not isinstance(raw[key], str):
            raise TypeError(key)
    return {
        "seq": raw["seq"],
        "at": raw["at"],
        "hub": raw["hub"],
        "kind": raw["kind"],
        "from_activity": raw["from_activity"],
        "to_activity": raw["to_activity"],
        "source": raw.get("source"),
        "noticed_late": bool(raw.get("noticed_late", False)),
        "previous_lasted_seconds": raw.get("previous_lasted_seconds"),
        "previous_lasted_at_least": bool(raw.get("previous_lasted_at_least", False)),
    }
