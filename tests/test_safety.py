"""Dry-run, rate limits, per-call caps, config validation and shutdown."""

from __future__ import annotations

import asyncio
import json

import pytest

from harmony_mcp.client import HarmonyError, HubClient
from harmony_mcp.config import HubSettings, load_settings
from harmony_mcp.hubs import HubRegistry
from harmony_mcp.ratelimit import TokenBucket, WindowLimit

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


def mute(c: HubClient):
    # The fake hub has Mute only in an activity's buttons; it still goes to the receiver.
    return c.catalog.find_command(c.catalog.activity("Watch Shield").commands, "Mute")


# --- dry-run ------------------------------------------------------------------------------
async def test_dry_run_sends_nothing_but_says_what_it_would(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build, dry_run=True)
    await c.start()
    await wait_until(lambda: c.available)
    o = await c.start_activity(c.catalog.activity("Watch Shield"))
    assert (o.dry_run, o.changed, o.sent) == (True, False, ["runactivity activityId=38000001 (Watch Shield)"])
    o = await c.send(mute(c), repeat=2, hold_ms=500)
    assert o.sent == ["holdAction press+release Onkyo AV Receiver/Mute, held 500 ms"] * 2
    assert fake.started == [] and fake.sent == []  # nothing reached the hub
    assert c.snapshot()["power"] == "off"  # and our state didn't pretend otherwise
    await c.stop()


async def test_dry_run_through_mcp(settings, factory, monkeypatch, fake):
    monkeypatch.setenv("HARMONY_DRY_RUN", "1")
    c = await connect(monkeypatch, factory)
    try:
        assert (await c.call_tool("harmony_get_status", {})).structured_content["dry_run"] is True
        r = (await c.call_tool("harmony_start_activity", {"activity": "Watch Shield"})).structured_content
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
    await hub.send(mute(hub), repeat=2, hold_ms=250, delay_ms=150)
    press = fake.sent[0]
    assert (press.device, press.command, press.delay) == (ONKYO, "Mute", 0.25)  # held 250 ms
    assert fake.sent[1] == 0.15  # a bare float is aioharmony's pause between presses


# TokenBucket itself is tested in test_ratelimit.py (shared); these test how the hub uses it.
async def test_runaway_presses_are_refused_whole(hub, fake):
    now = [0.0]
    hub.press_bucket = TokenBucket(capacity=5, rate=1.0, clock=lambda: now[0])
    await hub.send(volume_up(hub), repeat=4)
    with pytest.raises(HarmonyError, match="Rate limit on Living Room: refusing to press VolumeUp x3 .try again in 2s"):
        await hub.send(volume_up(hub), repeat=3)
    assert len(fake.presses()) == 4  # the refused call sent none of its three
    now[0] = 2.0
    await hub.send(volume_up(hub), repeat=3)


# --- the volume-up cap -------------------------------------------------------------------------
# A hub can't read the receiver's level, so every volume-up press makes it louder whatever the level
# already is. WindowLimit itself is tested in test_ratelimit.py (shared); these test how the hub uses it.
def capped(hub: HubClient, limit: int = 20) -> list[float]:
    now = [0.0]
    hub.volume_up_window = WindowLimit(limit, 300, clock=lambda: now[0])
    hub.press_bucket = TokenBucket(capacity=100, rate=100.0)  # out of the way: this is about the window
    return now


async def test_volume_up_is_capped_per_window(hub, fake):
    now = capped(hub)
    await hub.send(volume_up(hub), repeat=10)
    await hub.send(volume_up(hub), repeat=10)
    now[0] = 299.0
    with pytest.raises(HarmonyError) as refused:
        await hub.send(volume_up(hub))
    message = str(refused.value)
    for expected in [
        "Living Room",
        "at most 20 volume-up presses in any 5 minutes",
        "try again in 1s",
        "onkyo_set_volume",
        "webos_set_volume",
        "max_volume",
    ]:
        assert expected in message, expected
    assert len(fake.presses()) == 20  # the refused press was not sent
    now[0] = 300.0
    await hub.send(volume_up(hub), repeat=10)  # the first ten have left the window


async def test_volume_up_counts_every_repeat_and_refuses_whole(hub, fake):
    capped(hub, limit=5)
    await hub.send(volume_up(hub), repeat=4)
    with pytest.raises(HarmonyError, match="refusing to press VolumeUp x2"):
        await hub.send(volume_up(hub), repeat=2)
    assert len(fake.presses()) == 4


async def test_volume_down_and_mute_are_never_capped(hub, fake):
    capped(hub, limit=1)
    await hub.send(volume_up(hub))
    for _ in range(3):
        await hub.send(mute(hub))
    assert len(fake.presses()) == 4


async def test_a_held_volume_up_is_refused(hub, fake):
    # Most receivers ramp while the button is held, so one held press can be many steps.
    with pytest.raises(HarmonyError, match="can't be held"):
        await hub.send(volume_up(hub), hold_ms=500)
    assert fake.sent == []


async def test_volume_up_refused_by_the_press_limit_is_not_counted(hub, fake):
    capped(hub, limit=5)
    now = [0.0]
    hub.press_bucket = TokenBucket(capacity=3, rate=1.0, clock=lambda: now[0])
    with pytest.raises(HarmonyError, match="Rate limit on Living Room"):
        await hub.send(volume_up(hub), repeat=4)
    await hub.send(volume_up(hub), repeat=3)
    now[0] = 2.0
    await hub.send(volume_up(hub), repeat=2)  # 5 in the window: the refused 4 never counted


async def test_volume_up_through_the_activity_is_capped(two_hubs, factory, monkeypatch, fakes):
    monkeypatch.setenv("HARMONY_VOLUME_UP_LIMIT", "2")
    c = await connect(monkeypatch, factory)
    try:
        await c.call_tool("harmony_start_activity", {"activity": "Living Room/Watch Shield"})
        r = await c.call_tool("harmony_send_command", {"command": "volume up", "hub": "Living Room", "repeat": 2})
        assert not r.is_error
        r = await c.call_tool("harmony_send_command", {"command": "VolumeUp", "hub": "Living Room"})
        assert r.is_error and "at most 2 volume-up presses" in text(r)
    finally:
        await c.__aexit__(None, None, None)


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
    rows = (await two.call_tool("harmony_list_activities", {})).structured_content["result"]
    den_music = next(r["ref"] for r in rows if r["hub"] == "Den" and r["name"] == "Listen to Music")
    assert den_music == "Den/Listen to Music"
    assert not (await two.call_tool("harmony_start_activity", {"activity": den_music})).is_error
    assert fakes["192.0.2.21"].started == [39000002]


async def test_ref_and_conflicting_hub_argument_is_refused(two):
    result = await two.call_tool("harmony_start_activity", {"activity": "Den/Listen to Music", "hub": "Living Room"})
    assert result.is_error and "names hub Den, but hub='Living Room' was also given" in text(result)


async def test_a_name_containing_a_slash_that_reads_as_a_ref_is_ambiguous(two, fakes):
    living = fakes["192.0.2.20"]
    living.callbacks.config_updated(
        {"activity": [{"id": "1", "label": "Den/Listen to Music", "controlGroup": []}], "device": []}
    )
    result = await two.call_tool("harmony_start_activity", {"activity": "Den/Listen to Music"})
    assert result.is_error and "pass the name and hub separately" in text(result)


async def test_device_refs(two, fakes):
    result = await two.call_tool("harmony_send_command", {"command": "VolumeUp", "device": "Den/Den TV"})
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
    # Watch TV also routes the digit buttons (see test_channels.py), so only its first command is pinned.
    assert den["activities"][0]["ref"] == "Den/Watch TV" and den["activities"][0]["commands"][0] == "VolumeUp"
    living = json.loads((await two.read_resource("harmony://hubs/Living%20Room")).contents[0].text)
    assert living["hub"] == "Living Room"


async def test_resources_wait_out_the_startup_grace(config_dir, fakes, factory, monkeypatch):
    import harmony_mcp.server as server

    class SlowHub(fakes["192.0.2.20"].__class__):
        async def connect(self) -> bool:
            await asyncio.sleep(0.05)
            return await super().connect()

    fakes["192.0.2.20"] = SlowHub()
    monkeypatch.setattr(server, "HubRegistry", lambda s: HubRegistry(s, api_factory=factory))
    async with connect_without_wait(server.mcp) as c:
        doc = json.loads((await c.read_resource("harmony://hubs")).contents[0].text)
        assert doc[0]["reachable"] is True and doc[0]["activities"] != []
    fakes["192.0.2.20"] = SlowHub()
    async with connect_without_wait(server.mcp) as c:
        one = json.loads((await c.read_resource("harmony://hubs/Living%20Room")).contents[0].text)
        assert one["reachable"] is True and one["activities"] != []


def connect_without_wait(mcp_server):
    from mcp import Client

    return Client(mcp_server)


async def test_fix_stuck_remote_prompt(two):
    prompts = {p.name: p for p in (await two.list_prompts()).prompts}
    assert "fix_stuck_remote" in prompts
    got = await two.get_prompt("fix_stuck_remote", {"hub": "Den"})
    body = got.messages[0].content.text
    assert "the hub 'Den'" in body and "harmony_power_off" in body and "harmony_start_activity" in body


async def test_migrate_to_another_remote_prompt(two):
    prompts = {p.name: p for p in (await two.list_prompts()).prompts}
    assert "migrate_to_another_remote" in prompts
    body = (await two.get_prompt("migrate_to_another_remote", {"target": "Sofabaton X2"})).messages[0].content.text
    assert "'Sofabaton X2'" in body and "harmony_describe_activity" in body and "never fill it in" in body
    assert "not IR codes" in body and "export" in body
    body = (await two.get_prompt("migrate_to_another_remote", {})).messages[0].content.text
    assert "ask me which one" in body


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
                "volume_up_limit": 50,
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
        "ignoring volume_up_limit 50 in config.json: expected a whole number from 1 to 20",
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
        assert outcomes(await c.call_tool("harmony_power_off", {"hub": "all"})) == []
    finally:
        await c.__aexit__(None, None, None)


def test_volume_up_limit_setting(config_dir, monkeypatch):
    (config_dir / "config.json").write_text(json.dumps({"hubs": [{"host": "192.0.2.20"}], "volume_up_limit": 8}))
    assert load_settings().volume_up_limit == 8
    monkeypatch.setenv("HARMONY_VOLUME_UP_LIMIT", "5")
    assert load_settings().volume_up_limit == 5  # the environment wins
    monkeypatch.setenv("HARMONY_VOLUME_UP_LIMIT", "lots")
    s = load_settings()
    assert s.volume_up_limit == 8 and "ignoring HARMONY_VOLUME_UP_LIMIT='lots'" in "\n".join(s.problems)
    monkeypatch.delenv("HARMONY_VOLUME_UP_LIMIT")
    (config_dir / "config.json").write_text(json.dumps({"hubs": [{"host": "192.0.2.20"}]}))
    assert load_settings().volume_up_limit == 20
