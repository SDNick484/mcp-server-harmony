"""Dry-run, rate limits, per-call caps, config validation and shutdown."""

from __future__ import annotations

import asyncio
import json

import pytest

from harmony_mcp.client import HarmonyError, HubClient
from harmony_mcp.config import HubSettings, load_settings
from harmony_mcp.hubs import HubRegistry
from harmony_mcp.limits import TokenBucket

from .conftest import LIVING, ONKYO, wait_until
from .test_tools import connect, outcomes, text

pytestmark = pytest.mark.anyio


@pytest.fixture
async def hub(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    yield c
    await c.stop()


def volume_up(c: HubClient):
    return c.catalog.find_command(c.catalog.device("Onkyo AV Receiver").commands, "VolumeUp")


# --- dry-run ------------------------------------------------------------------------------
async def test_dry_run_sends_nothing_but_says_what_it_would(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build, dry_run=True)
    await c.start()
    await wait_until(lambda: c.available)
    o = await c.start_activity(c.catalog.activity("Watch Shield"))
    assert (o.dry_run, o.changed, o.sent) == (True, False, ["runactivity activityId=38000001 (Watch Shield)"])
    o = await c.send(volume_up(c), repeat=2, hold_ms=500)
    assert o.sent == ["holdAction press+release Onkyo AV Receiver/VolumeUp, held 500 ms"] * 2
    assert fake.started == [] and fake.sent == []  # nothing reached the hub
    assert c.snapshot()["power"] == "off"  # and our state didn't pretend otherwise
    await c.stop()


async def test_dry_run_through_mcp(settings, factory, monkeypatch, fake):
    monkeypatch.setenv("HARMONY_DRY_RUN", "1")
    c = await connect(monkeypatch, factory)
    try:
        assert (await c.call_tool("get_status", {})).structured_content["dry_run"] is True
        r = (await c.call_tool("start_activity", {"activity": "Watch Shield"})).structured_content
        assert r["outcome"] == "dry_run" and r["detail"].startswith("DRY RUN, nothing sent")
        assert fake.started == []
    finally:
        await c.__aexit__(None, None, None)


# --- caps and rate limits --------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"repeat": 11}, "repeat must be 1-10"),
        ({"hold_ms": 3001}, "hold_ms must be 0-3000"),
        ({"repeat": 2, "delay_ms": 50}, "delay_ms must be 100-2000"),
        ({"repeat": 10, "hold_ms": 1500, "delay_ms": 500}, "would take 19.5s"),
    ],
)
async def test_per_call_caps(hub, fake, kwargs, message):
    with pytest.raises(HarmonyError, match=message):
        await hub.send(volume_up(hub), **kwargs)
    assert fake.sent == []


async def test_hold_and_delay_reach_aioharmony(hub, fake):
    await hub.send(volume_up(hub), repeat=2, hold_ms=250, delay_ms=150)
    press = fake.sent[0]
    assert (press.device, press.command, press.delay) == (ONKYO, "VolumeUp", 0.25)  # held 250 ms
    assert fake.sent[1] == 0.15  # a bare float is aioharmony's pause between presses


def test_token_bucket_refills_over_time():
    now = [0.0]
    b = TokenBucket(capacity=3, rate=1.0, clock=lambda: now[0])
    assert [b.take() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.take() == pytest.approx(1.0)  # empty: wait one second for one token
    now[0] = 2.0
    assert b.take(2) == 0.0
    assert b.take(4) == float("inf")  # more than the bucket can ever hold


async def test_runaway_presses_are_refused_whole(hub, fake):
    now = [0.0]
    hub.press_bucket = TokenBucket(capacity=5, rate=1.0, clock=lambda: now[0])
    await hub.send(volume_up(hub), repeat=4)
    with pytest.raises(HarmonyError, match="Rate limit on Living Room: refusing to press VolumeUp x3 .try again in 2s"):
        await hub.send(volume_up(hub), repeat=3)
    assert len(fake.presses()) == 4  # the refused call sent none of its three
    now[0] = 2.0
    await hub.send(volume_up(hub), repeat=3)


async def test_activity_flapping_is_limited(hub, fake):
    now = [0.0]
    hub.activity_bucket = TokenBucket(capacity=2, rate=1 / 15, clock=lambda: now[0])
    await hub.start_activity(hub.catalog.activity("Watch Shield"))
    await hub.power_off()
    with pytest.raises(HarmonyError, match="Rate limit on Living Room: refusing to start Listen to Music"):
        await hub.start_activity(hub.catalog.activity("Listen to Music"))
    # Already-running / already-off are no-ops, so they never cost a token.
    assert (await hub.power_off()).changed is False


# --- hub-qualified references ---------------------------------------------------------------
@pytest.fixture
async def two(two_hubs, factory, monkeypatch):
    c = await connect(monkeypatch, factory)
    yield c
    await c.__aexit__(None, None, None)


async def test_refs_from_list_tools_work_as_names(two, fakes):
    rows = (await two.call_tool("list_activities", {})).structured_content["result"]
    den_music = next(r["ref"] for r in rows if r["hub"] == "Den" and r["name"] == "Listen to Music")
    assert den_music == "Den/Listen to Music"
    assert not (await two.call_tool("start_activity", {"activity": den_music})).is_error
    assert fakes["192.0.2.21"].started == [39000002]


async def test_ref_and_conflicting_hub_argument_is_refused(two):
    result = await two.call_tool("start_activity", {"activity": "Den/Listen to Music", "hub": "Living Room"})
    assert result.is_error and "names hub Den, but hub='Living Room' was also given" in text(result)


async def test_a_name_containing_a_slash_that_reads_as_a_ref_is_ambiguous(two, fakes):
    living = fakes["192.0.2.20"]
    living.callbacks.config_updated(
        {"activity": [{"id": "1", "label": "Den/Listen to Music", "controlGroup": []}], "device": []}
    )
    result = await two.call_tool("start_activity", {"activity": "Den/Listen to Music"})
    assert result.is_error and "pass the name and hub separately" in text(result)


async def test_device_refs(two, fakes):
    result = await two.call_tool("send_command", {"command": "VolumeUp", "device": "Den/Den TV"})
    assert not result.is_error and fakes["192.0.2.21"].presses() == [(72000001, "VolumeUp")]


# --- resources and prompts ----------------------------------------------------------------------
async def test_resources_list_the_catalog(two):
    listed = {str(r.uri) for r in (await two.list_resources()).resources}
    assert "harmony://hubs" in listed
    templates = {t.uri_template for t in (await two.list_resource_templates()).resource_templates}
    assert "harmony://hubs/{hub}" in templates
    doc = json.loads((await two.read_resource("harmony://hubs")).contents[0].text)
    assert [h["hub"] for h in doc] == ["Living Room", "Den"]
    den = json.loads((await two.read_resource("harmony://hubs/Den")).contents[0].text)
    assert den["activities"][0] == {"ref": "Den/Watch TV", "commands": ["VolumeUp"]}
    living = json.loads((await two.read_resource("harmony://hubs/Living%20Room")).contents[0].text)
    assert living["hub"] == "Living Room"


async def test_fix_stuck_remote_prompt(two):
    prompts = {p.name: p for p in (await two.list_prompts()).prompts}
    assert "fix_stuck_remote" in prompts
    got = await two.get_prompt("fix_stuck_remote", {"hub": "Den"})
    body = got.messages[0].content.text
    assert "the hub 'Den'" in body and "power_off" in body and "start_activity" in body


# --- config validation ------------------------------------------------------------------------------
def test_config_problems_are_sentences_not_crashes(config_dir):
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "hubs": [
                    {"host": "192.0.2.20", "name": "TV"},
                    {"host": "192.0.2.21:8088"},
                    {"host": "not a host!"},
                    {"host": "192.0.2.20"},
                    {"host": "hub-den.lan", "name": "tv"},
                ],
                "port": 99999,
                "protocol": "TELNET",
            }
        )
    )
    s = load_settings()
    assert [h.host for h in s.hubs] == ["192.0.2.20", "hub-den.lan"]
    joined = "\n".join(s.problems)
    for expected in [
        "includes a port; aioharmony always uses 8088",
        "'not a host!' is not an IP address or hostname",
        "192.0.2.20 is listed twice",
        "two hubs are named 'tv'",
        "ignoring port 99999",
        "ignoring protocol 'TELNET'",
    ]:
        assert expected in joined, expected


def test_invalid_json_is_reported_with_its_position(config_dir):
    (config_dir / "config.json").write_text('{"hubs": [}')
    s = load_settings()
    assert s.hubs == () and "is not valid JSON (line 1, column 11" in s.problems[0]


# --- shutdown -------------------------------------------------------------------------------------------
async def test_a_wedged_hub_cannot_hang_shutdown(settings, factory, monkeypatch):
    import harmony_mcp.hubs as hubs_module

    monkeypatch.setattr(hubs_module, "STOP_TIMEOUT", 0.1)
    reg = HubRegistry(settings, api_factory=factory)
    await reg.start()

    async def never() -> None:
        await asyncio.sleep(3600)

    reg.hubs[0].stop = never  # type: ignore[method-assign]
    await asyncio.wait_for(reg.stop(), timeout=2)  # returns after ~0.1 s, not an hour


async def test_power_off_with_nothing_on_returns_no_results(settings, factory, monkeypatch):
    c = await connect(monkeypatch, factory)
    try:
        assert outcomes(await c.call_tool("power_off", {"hub": "all"})) == []
    finally:
        await c.__aexit__(None, None, None)
