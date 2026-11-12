"""Activity history: what each hub changed from and to, where it came from, and that it survives a restart.

The clock is injected (FakeClock), so nothing here sleeps to make time pass.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from mcp import Client

from harmony_mcp import server
from harmony_mcp.client import HubClient
from harmony_mcp.config import HubSettings, load_settings
from harmony_mcp.history import HISTORY_FILE, History
from harmony_mcp.hubs import HubRegistry

from .conftest import DEN, DEN_WATCH, LISTEN_MUSIC, LIVING, WATCH_SHIELD, wait_until

pytestmark = pytest.mark.anyio


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 21, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def history(clock) -> History:
    return History(None, clock=clock)


@pytest.fixture
async def hub(fake, history):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build, history=history)
    await c.start()
    await wait_until(lambda: c.available)
    yield c
    await c.stop()


def kinds(history: History) -> list[tuple[str, str | None, str | None, str | None]]:
    """(kind, from, to, source) oldest first."""
    return [(e["kind"], e["from_activity"], e["to_activity"], e["source"]) for e in history._events]


def push(fake, activity_id: int) -> None:
    """The hub pushes a change it made (remote, app, another client): no call from us."""
    fake.activity_id = activity_id
    assert fake.callbacks is not None
    fake.callbacks.new_activity((activity_id, None))


class Refresh:
    """HarmonyAPI._harmony_client: a re-read that changes nothing by itself (the fake's state is the hub's)."""

    async def refresh_info_from_hub(self) -> None:
        return None


# --- recording ---------------------------------------------------------------------------
async def test_the_first_state_read_is_a_baseline_not_a_change(hub, history):
    assert kinds(history) == [("recording_started", None, None, None)]
    (e,) = history._events
    assert e["hub"] == "Living Room" and e["noticed_late"] is False and e["previous_lasted_seconds"] is None
    assert e["at"] == "2026-11-12T21:00:00+00:00"


async def test_a_baseline_on_a_running_activity_names_it(fake, history):
    fake.activity_id = WATCH_SHIELD
    c = HubClient(HubSettings(LIVING), api_factory=fake.build, history=history)
    await c.start()
    await wait_until(lambda: c.available)
    assert kinds(history) == [("recording_started", None, "Watch Shield", None)]
    await c.stop()


async def test_a_change_this_server_made(hub, history, clock):
    clock.advance(minutes=5)
    await hub.start_activity(hub.catalog.activity("Watch Shield"))
    assert kinds(history)[1:] == [("change", None, "Watch Shield", "this_server")]
    e = history._events[1]
    assert e["previous_lasted_seconds"] == 300
    # The previous state (off) began before recording did, so 300 s is a minimum.
    assert e["previous_lasted_at_least"] is True and e["noticed_late"] is False


async def test_a_change_pushed_from_elsewhere(hub, fake, history, clock):
    await hub.start_activity(hub.catalog.activity("Watch Shield"))
    clock.advance(hours=2)
    push(fake, LISTEN_MUSIC)  # someone used the remote
    e = history._events[-1]
    assert (e["from_activity"], e["to_activity"], e["source"]) == ("Watch Shield", "Listen to Music", "elsewhere")
    assert e["previous_lasted_seconds"] == 7200
    assert e["previous_lasted_at_least"] is False  # we saw Watch Shield begin


async def test_a_power_off_from_elsewhere_is_a_change_to_off(hub, fake, history):
    await hub.start_activity(hub.catalog.activity("Watch Shield"))
    push(fake, -1)
    assert kinds(history)[-1] == ("change", "Watch Shield", None, "elsewhere")


async def test_our_power_off_is_ours(hub, history):
    await hub.start_activity(hub.catalog.activity("Watch Shield"))
    await hub.power_off()
    assert kinds(history)[-1] == ("change", "Watch Shield", None, "this_server")


async def test_a_change_is_recorded_once_when_both_paths_fire(hub, fake, history):
    # The fake pushes new_activity inside start_activity *and* _run_activity sets the state after the reply.
    await hub.start_activity(hub.catalog.activity("Watch Shield"))
    assert fake.callbacks is not None
    fake.callbacks.new_activity((WATCH_SHIELD, None))  # a late duplicate push, after the reply
    assert [k[0] for k in kinds(history)] == ["recording_started", "change"]


async def test_a_push_while_our_start_waits_is_still_ours(hub, fake, history):
    # Whichever of the push and the reply arrives first, the source is this server.
    fake.start_result = (True, "OK")  # the fake sends no push of its own for this
    hub._own_target = WATCH_SHIELD
    push(fake, WATCH_SHIELD)
    assert kinds(history)[-1][3] == "this_server"


async def test_a_change_found_after_a_reconnect_is_marked_noticed_late(hub, fake, history, clock):
    fake._harmony_client = Refresh()  # type: ignore[attr-defined]
    fake.push_disconnect()
    fake.activity_id = LISTEN_MUSIC  # changed while the socket was down: no push reached us
    clock.advance(minutes=10)
    assert fake.callbacks is not None
    fake.callbacks.connect(LIVING)
    await wait_until(lambda: hub.activity_id == LISTEN_MUSIC)
    e = history._events[-1]
    assert (e["kind"], e["to_activity"], e["source"], e["noticed_late"]) == (
        "change",
        "Listen to Music",
        "elsewhere",
        True,
    )
    assert e["at"] == "2026-11-12T21:10:00+00:00"  # when it was noticed


async def test_a_reconnect_that_finds_nothing_new_records_nothing(hub, fake, history):
    fake._harmony_client = Refresh()  # type: ignore[attr-defined]
    fake.push_disconnect()
    assert fake.callbacks is not None
    fake.callbacks.connect(LIVING)
    await wait_until(lambda: hub._resync_task is not None and hub._resync_task.done())
    assert [k[0] for k in kinds(history)] == ["recording_started"]


async def test_a_hub_without_a_name_is_recorded_by_masked_address_never_the_address(fake, history):
    fake.name = DEN  # aioharmony names a hub by its IP when it has no friendlyName
    c = HubClient(HubSettings(DEN), api_factory=fake.build, history=history)
    await c.start()
    await wait_until(lambda: c.available)
    assert history._events[0]["hub"] == "x.x.x.21"
    await c.stop()


async def test_dry_run_changes_nothing_so_records_nothing(fake, history):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build, dry_run=True, history=history)
    await c.start()
    await wait_until(lambda: c.available)
    await c.start_activity(c.catalog.activity("Watch Shield"))
    assert [k[0] for k in kinds(history)] == ["recording_started"]
    await c.stop()


# --- the cap and paging ------------------------------------------------------------------
def fill(history: History, n: int) -> None:
    history.baseline("Den", None)
    for i in range(n):
        history.change("Den", "A" if i % 2 == 0 else None, "elsewhere")


def test_memory_is_capped_and_the_oldest_go_first(clock):
    h = History(None, clock=clock, max_events=4)
    fill(h, 9)  # 10 events, seq 1..10
    assert [e["seq"] for e in h._events] == [7, 8, 9, 10]
    assert h.recording_since == h._events[0]["at"]
    events, expired = h.page(None, 0, 10)
    assert expired is True and [e["seq"] for e in events] == [7, 8, 9, 10]


def test_paging_newest_first_then_oldest_first_after_a_seq(history):
    fill(history, 5)  # seq 1..6
    newest, expired = history.page(None, None, 2)
    assert [e["seq"] for e in newest] == [6, 5] and expired is False
    caught_up, expired = history.page(None, 3, 2)
    assert [e["seq"] for e in caught_up] == [4, 5] and expired is False
    assert history.page(None, 6, 5) == ([], False)
    assert history.page(None, 99, 5)[1] is True  # ahead of us: the counter restarted


def test_the_hub_filter_keeps_the_global_last_seq(history):
    history.baseline("Den", None)
    history.baseline("Office", None)
    events, _ = history.page("Den", None, 10)
    assert [e["hub"] for e in events] == ["Den"] and history.last_seq == 2


# --- the file ----------------------------------------------------------------------------
def test_events_survive_a_restart_and_seq_continues(tmp_path, clock):
    path = tmp_path / HISTORY_FILE
    first = History(path, clock=clock)
    first.baseline("Den", None)
    clock.advance(minutes=1)
    first.change("Den", "Watch TV", "elsewhere")

    second = History(path, clock=clock)  # a restart: nothing in memory but what the file held
    assert [e["seq"] for e in second._events] == [1, 2]
    assert second._events[1]["to_activity"] == "Watch TV"
    second.baseline("Den", "Watch TV")  # the new session's baseline, appended after the old events
    assert [e["seq"] for e in History(path, clock=clock)._events] == [1, 2, 3]
    assert second.page("Den", None, 5)[0][0]["kind"] == "recording_started"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_file_is_owner_only(tmp_path, clock):
    path = tmp_path / "sub" / HISTORY_FILE
    History(path, clock=clock).baseline("Den", None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_file_is_capped_and_stays_owner_only_after_compaction(tmp_path, clock):
    path = tmp_path / HISTORY_FILE
    h = History(path, clock=clock, max_events=4, file_slack=2)
    fill(h, 12)
    lines = path.read_text().splitlines()
    assert len(lines) <= 4 + 2  # compacted whenever it passed cap + slack
    assert json.loads(lines[-1])["seq"] == h.last_seq  # the newest is always there
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [e["seq"] for e in History(path, clock=clock, max_events=4)._events] == [10, 11, 12, 13]
    assert not list(tmp_path.glob(".history-*"))  # no temp files left behind


def test_unreadable_lines_are_skipped_not_fatal(tmp_path, clock, caplog):
    path = tmp_path / HISTORY_FILE
    good = History(path, clock=clock)
    good.baseline("Den", None)
    with path.open("a") as f:
        f.write('{"seq": 2, "at": "x"\n')  # torn by a crash mid-write
        f.write('{"kind": "change"}\n')
        f.write("[1]\n")
    with caplog.at_level(logging.WARNING, logger="harmony_mcp.history"):
        again = History(path, clock=clock)
    assert [e["seq"] for e in again._events] == [1]
    assert "skipped 3 unreadable" in caplog.text


def test_records_hold_names_only(tmp_path, clock):
    path = tmp_path / HISTORY_FILE
    h = History(path, clock=clock)
    h.baseline("192.0.2.20", None)  # a hub known only by its address
    h.change("192.0.2.20", "Watch TV", "elsewhere")
    assert "192.0.2.20" not in path.read_text()


# --- off, and unwritable -----------------------------------------------------------------
def test_turned_off_records_nothing_anywhere(tmp_path, clock):
    path = tmp_path / HISTORY_FILE
    h = History(path, enabled=False, clock=clock)
    h.baseline("Den", None)
    assert h.change("Den", "A", "elsewhere") is False
    assert len(h._events) == 0 and not path.exists() and h.persisting is False


def test_the_off_switch_in_env_and_config(config_dir, monkeypatch):
    assert load_settings().history is True
    (config_dir / "config.json").write_text(json.dumps({"hubs": [{"host": LIVING}], "history": False}))
    assert load_settings().history is False
    monkeypatch.setenv("HARMONY_HISTORY", "on")  # the environment wins over the file
    assert load_settings().history is True
    monkeypatch.setenv("HARMONY_HISTORY", "off")
    assert load_settings().history is False


def test_a_bad_off_switch_is_a_problem_and_leaves_history_on(config_dir, monkeypatch):
    monkeypatch.setenv("HARMONY_HISTORY", "maybe")
    s = load_settings()
    assert s.history is True and any("HARMONY_HISTORY" in p for p in s.problems)
    monkeypatch.delenv("HARMONY_HISTORY")
    (config_dir / "config.json").write_text(json.dumps({"hubs": [{"host": LIVING}], "history": "no"}))
    s = load_settings()
    assert s.history is True and any("config.json" in p and "history" in p for p in s.problems)


def test_an_unwritable_directory_is_logged_once_and_history_keeps_working(tmp_path, clock, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the config directory should be")
    with caplog.at_level(logging.WARNING, logger="harmony_mcp.history"):
        h = History(blocker / HISTORY_FILE, clock=clock)
        h.baseline("Den", None)
        h.change("Den", "A", "elsewhere")
        h.change("Den", None, "elsewhere")
    assert [e["seq"] for e in h._events] == [1, 2, 3]  # still in memory
    assert caplog.text.count("History: couldn't") == 1  # one warning, not one per event
    assert h.persisting is False


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
def test_a_read_only_directory_is_the_same(tmp_path, clock, caplog):
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        h = History(d / HISTORY_FILE, clock=clock)
        with caplog.at_level(logging.WARNING, logger="harmony_mcp.history"):
            h.baseline("Den", None)
        assert h.last_seq == 1 and "couldn't write" in caplog.text
    finally:
        d.chmod(0o700)


async def test_a_write_failure_never_breaks_a_start(fake, tmp_path, clock):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    h = History(blocker / HISTORY_FILE, clock=clock)
    c = HubClient(HubSettings(LIVING), api_factory=fake.build, history=h)
    await c.start()
    await wait_until(lambda: c.available)
    outcome = await c.start_activity(c.catalog.activity("Watch Shield"))
    assert outcome.changed and c.activity_id == WATCH_SHIELD
    await c.stop()


# --- the tool ----------------------------------------------------------------------------
@pytest.fixture
async def mcp_client(two_hubs, factory, monkeypatch, history):
    monkeypatch.setattr(server, "HubRegistry", lambda s: HubRegistry(s, api_factory=factory, history=history))
    c = Client(server.mcp)
    await c.__aenter__()
    await wait_until(lambda: all(h.available for h in server.hubs().hubs))
    yield c
    await c.__aexit__(None, None, None)


async def get_history(c: Client, **args):
    result = await c.call_tool("harmony_get_history", args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_the_tool_reports_baselines_then_changes_newest_first(mcp_client, history, clock):
    clock.advance(minutes=3)
    r = await mcp_client.call_tool("harmony_start_activity", {"activity": "Den/Watch TV"})
    assert not r.is_error
    page = await get_history(mcp_client)
    assert page["enabled"] is True and page["persisted"] is False and page["expired"] is False
    assert page["session_started"] == "2026-11-12T21:00:00+00:00"
    assert page["recording_since"] == "2026-11-12T21:00:00+00:00"
    assert [e["kind"] for e in page["events"]] == ["change", "recording_started", "recording_started"]
    newest = page["events"][0]
    assert (newest["kind"], newest["hub"], newest["to_activity"], newest["source"]) == (
        "change",
        "Den",
        "Watch TV",
        "this_server",
    )
    assert page["last_seq"] == newest["seq"]


async def test_the_tool_polls_from_last_seq_oldest_first(mcp_client, fakes):
    first = await get_history(mcp_client)
    push(fakes[DEN], DEN_WATCH)
    more = await get_history(mcp_client, after_seq=first["last_seq"])
    assert [e["kind"] for e in more["events"]] == ["change"]
    assert more["events"][0]["source"] == "elsewhere" and more["events"][0]["hub"] == "Den"
    assert (await get_history(mcp_client, after_seq=more["last_seq"]))["events"] == []


async def test_the_tool_filters_by_hub_and_rejects_an_unknown_one(mcp_client):
    page = await get_history(mcp_client, hub="Den")
    assert {e["hub"] for e in page["events"]} == {"Den"}
    bad = await mcp_client.call_tool("harmony_get_history", {"hub": "Garage"})
    assert bad.is_error and "No hub named" in bad.content[0].text


async def test_the_tool_limits_its_page(mcp_client, history):
    for i in range(30):
        history.change("Den", f"A{i}", "elsewhere")
    assert len((await get_history(mcp_client, limit=5))["events"]) == 5
    assert len((await get_history(mcp_client))["events"]) == 20
    too_many = await mcp_client.call_tool("harmony_get_history", {"limit": 101})
    assert too_many.is_error


async def test_the_tool_says_when_history_is_off(two_hubs, factory, monkeypatch, clock):
    off = History(None, enabled=False, clock=clock)
    monkeypatch.setattr(server, "HubRegistry", lambda s: HubRegistry(s, api_factory=factory, history=off))
    async with Client(server.mcp) as c:
        await wait_until(lambda: all(h.available for h in server.hubs().hubs))
        page = await get_history(c)
    assert page["enabled"] is False and page["events"] == [] and "turned off" in page["hint"]


# --- one-shot `call` is read-only ----------------------------------------------------------
def test_a_read_only_history_loads_the_file_but_records_nothing(tmp_path, clock):
    path = tmp_path / HISTORY_FILE
    server_side = History(path, clock=clock)
    server_side.baseline("Den", None)
    server_side.change("Den", "Watch TV", "this_server")
    before = path.read_bytes()

    reader = History(path, clock=clock, read_only=True)
    reader.baseline("Den", "Watch TV")  # what a `call`'s first state read does
    reader.baseline("Living Room", None)
    reader.change("Den", None, "elsewhere")

    assert [e["kind"] for e in reader._events] == ["recording_started", "change"]  # only what was on disk
    assert reader.last_seq == 2
    assert path.read_bytes() == before


def test_a_torn_last_line_never_breaks_a_reader(tmp_path, clock):
    """A running server may be mid-append while `call` reads: even a cut inside a multi-byte character."""
    path = tmp_path / HISTORY_FILE
    History(path, clock=clock).baseline("Den", "Watch TV")
    whole = json.dumps(
        {"seq": 2, "at": "x", "hub": "Den", "kind": "change", "to_activity": "Café"}, ensure_ascii=False
    ).encode()
    cut = whole.index("é".encode()) + 1  # half of the two-byte é
    with path.open("ab") as f:
        f.write(b"\n" + whole[:cut])  # no trailing newline: still being written
    reader = History(path, clock=clock, read_only=True)
    assert [e["seq"] for e in reader._events] == [1]


async def test_call_leaves_no_marker_and_get_history_returns_what_a_server_recorded(
    two_hubs, factory, monkeypatch, capsys, clock
):
    import argparse

    from harmony_mcp import cli

    path = two_hubs / HISTORY_FILE
    recorder = History(path, clock=clock)  # what a running `serve` wrote earlier
    recorder.baseline("Den", None)
    clock.advance(minutes=5)
    recorder.change("Den", "Watch TV", "elsewhere")
    before = path.read_bytes()
    monkeypatch.setattr(server, "HubRegistry", lambda s: HubRegistry(s, api_factory=factory))

    for _ in range(2):
        assert await cli._cmd_call(argparse.Namespace(tool="get_status", args=[])) == 0
    capsys.readouterr()  # drop the status output; keep only get_history's
    assert await cli._cmd_call(argparse.Namespace(tool="get_history", args=[])) == 0
    page = json.loads(capsys.readouterr().out)

    assert path.read_bytes() == before  # no recording_started per hub, per call
    assert [(e["kind"], e["to_activity"], e["source"]) for e in page["events"]] == [
        ("change", "Watch TV", "elsewhere"),
        ("recording_started", None, None),
    ]
    assert server.record_history is True  # restored, so serve in the same process still records
