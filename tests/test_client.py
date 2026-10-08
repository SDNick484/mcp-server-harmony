"""HubClient against the fake hub: connection lifecycle, state, and commands."""

from __future__ import annotations

import pytest
from aioharmony.exceptions import TimeOut

from harmony_mcp.client import HarmonyError, HubClient
from harmony_mcp.config import Settings

from .conftest import ONKYO, WATCH_SHIELD, wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
async def hub(settings, fake):
    c = HubClient(settings, api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    yield c
    await c.stop()


async def test_connects_with_settings(hub, fake):
    assert fake.built_with == {"ip_address": "192.0.2.20", "protocol": None}
    assert hub.snapshot()["power"] == "off"
    assert len(hub.catalog.activities) == 2


async def test_retries_until_the_hub_answers(settings, fake):
    fake.connect_results = [False, TimeOut(), OSError("no route"), True]
    c = HubClient(settings, api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    assert fake.connect_calls == 4
    await c.stop()


async def test_unconfigured_never_connects(fake):
    c = HubClient(Settings(host=None), api_factory=fake.build)
    await c.start()
    assert fake.connect_calls == 0
    with pytest.raises(HarmonyError, match="HARMONY_HOST"):
        await c.power_off()


async def test_start_activity_tracks_state(hub, fake):
    a = hub.catalog.activity("Watch Shield")
    assert await hub.start_activity(a) is True
    assert fake.started == [WATCH_SHIELD]
    snap = hub.snapshot()
    assert snap["current_activity"] == {"id": WATCH_SHIELD, "name": "Watch Shield"}
    assert (snap["power"], snap["transition"]) == ("on", None)


async def test_starting_the_running_activity_sends_nothing(hub, fake):
    fake.activity_id = WATCH_SHIELD
    hub.activity_id = WATCH_SHIELD
    assert await hub.start_activity(hub.catalog.activity("Watch Shield")) is False
    assert fake.started == []


async def test_power_off_is_idempotent(hub, fake):
    assert await hub.power_off() is False  # already off at start
    hub.activity_id = WATCH_SHIELD
    assert await hub.power_off() is True
    assert fake.started == [-1]


async def test_refused_activity_reports_the_hubs_reason(hub, fake):
    fake.start_result = (False, "Device not responding")
    with pytest.raises(HarmonyError, match="Device not responding"):
        await hub.start_activity(hub.catalog.activity("Watch Shield"))


async def test_slow_activity_times_out_with_advice(hub, fake):
    fake.start_hangs = True
    with pytest.raises(HarmonyError, match="check get_status"):
        await hub.start_activity(hub.catalog.activity("Watch Shield"))


async def test_commands_wait_out_an_activity_sequence(hub, fake):
    fake.push_starting(WATCH_SHIELD)
    assert hub.snapshot()["transition"] == "starting Watch Shield"
    cmd = hub.catalog.device("Onkyo AV Receiver").commands[0]
    with pytest.raises(HarmonyError, match="busy starting Watch Shield"):
        await hub.send(cmd)
    assert fake.sent == []


async def test_send_repeats_with_pauses_between(hub, fake, monkeypatch):
    monkeypatch.setattr(HubClient, "repeat_gap", 0.25)
    cmd = hub.catalog.find_command(hub.catalog.device("Onkyo AV Receiver").commands, "VolumeUp")
    await hub.send(cmd, repeat=3)
    assert fake.presses() == [(ONKYO, "VolumeUp")] * 3
    # aioharmony treats a bare float in the list as "sleep this long".
    assert [x for x in fake.sent if isinstance(x, float)] == [0.25, 0.25]


async def test_hub_rejection_is_reported(hub, fake):
    fake.failing_commands = {"VolumeUp"}
    cmd = hub.catalog.find_command(hub.catalog.device("Onkyo AV Receiver").commands, "VolumeUp")
    with pytest.raises(HarmonyError, match="rejected VolumeUp for Onkyo AV Receiver"):
        await hub.send(cmd)


async def test_disconnect_and_config_change(hub, fake):
    fake.push_disconnect()
    assert hub.snapshot()["reachable"] is False
    with pytest.raises(HarmonyError, match="Can't reach the Harmony hub at 192.0.2.20"):
        await hub.power_off()
    fake.callbacks.config_updated({"activity": [{"id": "5", "label": "Game"}], "device": []})
    assert [a.name for a in hub.catalog.activities] == ["Game"]


async def test_stop_closes_the_connection(settings, fake):
    c = HubClient(settings, api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    await c.stop()
    assert fake.closed and not c.available
