"""The sleep timer: "turn the TV off in 45 minutes", held by this server.

Durations shrink the way retry_delay does: ``HubClient.sleep_unit`` is how many seconds one "minute" lasts, so
a 1-minute timer takes 50 ms and nothing here sleeps for real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from harmony_mcp.client import HubClient
from harmony_mcp.config import HubSettings

from .conftest import LIVING, WATCH_SHIELD
from .test_multi_hub import mcp_client as two_hub_client  # noqa: F401  (fixture)
from .test_tools import connect, text

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def tiny_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HubClient, "sleep_unit", 0.05)
    monkeypatch.setattr(HubClient, "sleep_retry_window", 0.3)
    monkeypatch.setattr(HubClient, "sleep_retry_delay", 0.02)


@pytest.fixture
async def mcp_client(settings, factory, monkeypatch):
    c = await connect(monkeypatch, factory)
    yield c
    await c.__aexit__(None, None, None)


async def eventually(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def status(c, index: int = 0) -> dict:
    return (await c.call_tool("harmony_get_status", {})).structured_content["hubs"][index]


async def turn_on(c, activity: str = "Watch Shield", **kw) -> None:
    await c.call_tool("harmony_start_activity", {"activity": activity, **kw})


async def set_timer(c, minutes: int, **kw):
    return await c.call_tool("harmony_set_sleep_timer", {"minutes": minutes, **kw})


# --- the happy path -------------------------------------------------------------------------
async def test_it_fires_and_the_hub_powers_off(mcp_client, fake):
    await turn_on(mcp_client)
    result = await set_timer(mcp_client, 2)
    assert result.structured_content["outcome"] == "set"
    pending = (await status(mcp_client))["sleep_timer"]
    assert pending["minutes_left"] in (1, 2) and pending["fires_at"].endswith("+00:00") and pending["retrying"] is None
    await eventually(lambda: fake.started == [WATCH_SHIELD, -1])
    await eventually(lambda: server_hub().sleep_last is not None)
    s = await status(mcp_client)
    assert s["power"] == "off" and s["sleep_timer"] is None
    assert s["last_sleep_timer"]["outcome"] == "fired"


def server_hub(index: int = 0) -> HubClient:
    from harmony_mcp import server

    return server.hubs().hubs[index]


async def test_the_result_says_the_timer_lives_in_memory(mcp_client):
    await turn_on(mcp_client)
    detail = (await set_timer(mcp_client, 30)).structured_content["detail"]
    assert "memory" in detail and "serve --http" in detail and "stdio" in detail


async def test_replace_keeps_one_timer_at_the_new_time(mcp_client, fake):
    await turn_on(mcp_client)
    await set_timer(mcp_client, 200)  # would fire in 10 s if it weren't replaced
    result = await set_timer(mcp_client, 2)
    assert "replaced" in result.structured_content["detail"]
    await eventually(lambda: fake.started == [WATCH_SHIELD, -1])
    s = await status(mcp_client)
    assert s["last_sleep_timer"]["outcome"] == "fired"  # the replacement fired; the old one never did
    assert server_hub().sleep_last["outcome"] == "fired"


async def test_minutes_zero_cancels(mcp_client, fake):
    await turn_on(mcp_client)
    await set_timer(mcp_client, 2)
    result = await set_timer(mcp_client, 0)
    assert result.structured_content["outcome"] == "cancelled" and result.structured_content["sleep_timer"] is None
    s = await status(mcp_client)
    assert s["sleep_timer"] is None and s["last_sleep_timer"]["outcome"] == "cancelled"
    await asyncio.sleep(0.2)  # well past when it would have fired
    assert fake.started == [WATCH_SHIELD]


async def test_cancelling_with_no_timer_says_so(mcp_client):
    await turn_on(mcp_client)
    result = await set_timer(mcp_client, 0)
    assert result.is_error and "No sleep timer is set" in text(result)


async def test_a_hub_that_is_off_is_refused(mcp_client):
    result = await set_timer(mcp_client, 5)
    assert result.is_error and "No hub is on" in text(result)
    result = await set_timer(mcp_client, 5, hub="Living Room")
    assert result.is_error and "is off, so there is nothing to turn off later" in text(result)


async def test_the_range_is_capped(mcp_client):
    props = next(t for t in (await mcp_client.list_tools()).tools if t.name == "harmony_set_sleep_timer")
    minutes = props.input_schema["properties"]["minutes"]
    assert (minutes["minimum"], minutes["maximum"]) == (0, 720)
    assert (await set_timer(mcp_client, 721)).is_error


# --- the hub changes under the timer ----------------------------------------------------------
async def test_the_hub_going_off_first_drops_the_timer(mcp_client, fake):
    await turn_on(mcp_client)
    await set_timer(mcp_client, 3)
    await mcp_client.call_tool("harmony_power_off", {})  # or anyone else; the hub reports it the same way
    s = await status(mcp_client)
    assert s["sleep_timer"] is None
    assert s["last_sleep_timer"]["outcome"] == "cancelled" and "powered off before" in s["last_sleep_timer"]["detail"]
    await asyncio.sleep(0.25)
    assert fake.started == [WATCH_SHIELD, -1]  # one power off, the person's; the timer never sent another


async def test_the_hub_powered_off_from_the_remote_drops_it_too(mcp_client, fake):
    await turn_on(mcp_client)
    await set_timer(mcp_client, 3)
    fake.callbacks.new_activity((-1, None))  # pushed by the hub when someone presses Off on the remote
    assert (await status(mcp_client))["sleep_timer"] is None


async def test_switching_activities_keeps_it(mcp_client, fake):
    await turn_on(mcp_client)
    await set_timer(mcp_client, 3)
    await turn_on(mcp_client, "Listen to Music")
    assert (await status(mcp_client))["sleep_timer"] is not None
    await eventually(lambda: fake.started[-1] == -1)
    assert server_hub().sleep_last["outcome"] == "fired"


async def test_busy_then_retry(mcp_client, fake):
    await turn_on(mcp_client)
    fake.push_starting(WATCH_SHIELD)  # a start from the remote is mid-sequence when the timer is due
    await set_timer(mcp_client, 1)
    await eventually(lambda: server_hub()._timer is not None and server_hub()._timer.retry_reason is not None)
    s = await status(mcp_client)
    assert "busy starting" in s["sleep_timer"]["retrying"] and s["sleep_timer"]["minutes_left"] == 0
    assert fake.started == [WATCH_SHIELD]  # nothing sent into the sequence
    fake.callbacks.new_activity((WATCH_SHIELD, None))  # the sequence ends
    await eventually(lambda: fake.started == [WATCH_SHIELD, -1])
    await eventually(lambda: server_hub().sleep_last is not None)
    assert server_hub().sleep_last["outcome"] == "fired"


async def test_an_unreachable_hub_gives_up_with_the_reason(mcp_client, fake):
    await turn_on(mcp_client)
    await set_timer(mcp_client, 1)
    fake.push_disconnect()
    await eventually(lambda: server_hub().sleep_last is not None)
    s = await status(mcp_client)
    assert s["sleep_timer"] is None
    last = s["last_sleep_timer"]
    assert last["outcome"] == "failed" and "Can't reach the Harmony hub" in last["detail"]
    assert fake.started == [WATCH_SHIELD]


async def test_a_refused_power_off_is_a_failure_with_the_hubs_reason(mcp_client, fake, monkeypatch):
    # Every try that reaches the hub takes an activity rate-limit token, so keep the tries few here (the real
    # 15 s gap is exactly the bucket's refill rate, so a real run never starves itself).
    monkeypatch.setattr(HubClient, "sleep_retry_window", 0.08)
    monkeypatch.setattr(HubClient, "sleep_retry_delay", 0.05)
    await turn_on(mcp_client)
    fake.start_result = (False, "Activity in progress")
    await set_timer(mcp_client, 1)
    await eventually(lambda: server_hub().sleep_last is not None)
    assert "Activity in progress" in server_hub().sleep_last["detail"]


# --- dry run, shutdown ------------------------------------------------------------------------
async def test_dry_run_reports_the_power_off_and_sends_nothing(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build, dry_run=True)
    await c.start()
    await eventually(lambda: c.available)
    c.activity_id = WATCH_SHIELD  # a dry run never starts anything, so say it is on
    await c.set_sleep_timer(1)
    await eventually(lambda: c.sleep_last is not None)
    assert c.sleep_last["outcome"] == "fired" and "DRY RUN" in c.sleep_last["detail"]
    assert fake.started == []
    await c.stop()


async def test_stopping_the_client_cancels_the_timer(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build)
    await c.start()
    await eventually(lambda: c.available)
    c.activity_id = WATCH_SHIELD
    await c.set_sleep_timer(2)
    task = c._timer.task
    await c.stop()
    assert task.done() and c.snapshot()["sleep_timer"] is None
    await asyncio.sleep(0.2)
    assert fake.started == []


# --- several hubs ------------------------------------------------------------------------------
async def test_two_hubs_on_need_a_hub(two_hub_client, fakes):  # noqa: F811
    c = two_hub_client
    await turn_on(c, "Watch Shield")
    await turn_on(c, "Watch TV")
    result = await set_timer(c, 5)
    assert result.is_error and "Several hubs are on (Living Room, Den); pass hub" in text(result)
    assert all(not f.started[1:] for f in fakes.values())
    assert not (await set_timer(c, 5, hub="Den")).is_error
    hubs = (await c.call_tool("harmony_get_status", {})).structured_content["hubs"]
    assert [h["sleep_timer"] is not None for h in hubs] == [False, True]


async def test_one_hub_on_is_found_without_a_name(two_hub_client, fakes):  # noqa: F811
    c = two_hub_client
    await turn_on(c, "Watch TV")
    assert (await set_timer(c, 5)).structured_content["hub"] == "Den"
    assert (await set_timer(c, 0)).structured_content["outcome"] == "cancelled"  # cancel finds the one timer


async def test_cancel_with_two_timers_asks(two_hub_client):  # noqa: F811
    c = two_hub_client
    await turn_on(c, "Watch Shield")
    await turn_on(c, "Watch TV")
    await set_timer(c, 5, hub="Den")
    await set_timer(c, 5, hub="Living Room")
    result = await set_timer(c, 0)
    assert result.is_error and "Sleep timers are set on" in text(result)
    assert not (await set_timer(c, 0, hub="Den")).is_error
