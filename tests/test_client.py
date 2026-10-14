"""HubClient against the fake hub: connection lifecycle, state, and commands."""

from __future__ import annotations

import asyncio

import pytest
from aioharmony.exceptions import TimeOut

from harmony_mcp.client import HarmonyError, HubClient
from harmony_mcp.config import HubSettings
from harmony_mcp.hubs import HubRegistry

from .conftest import LIVING, ONKYO, WATCH_SHIELD, FakeHarmonyAPI, wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
async def hub(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    yield c
    await c.stop()


async def test_connects_with_settings(hub, fake):
    assert fake.built_with == {"ip_address": "192.0.2.20", "protocol": None}
    assert hub.snapshot()["power"] == "off"
    assert len(hub.catalog.activities) == 2


async def test_retries_until_the_hub_answers(fake):
    fake.connect_results = [False, TimeOut(), OSError("no route"), True]
    c = HubClient(HubSettings(LIVING), api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    assert fake.connect_calls == 4
    await c.stop()


async def test_name_comes_from_config_then_hub_then_address(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build)
    assert c.name == LIVING  # not connected yet
    await c.start()
    await wait_until(lambda: c.available)
    assert c.name == "Living Room"  # the hub's friendlyName
    assert c.answers_to("living room") and c.answers_to(LIVING) and not c.answers_to("Den")
    await c.stop()
    named = HubClient(HubSettings(LIVING, "Family Room"), api_factory=fake.build)
    assert named.name == "Family Room"


async def test_offline_hub_errors_name_it(fake):
    fake.connect_results = [False] * 1000
    c = HubClient(HubSettings(LIVING, "Family Room"), api_factory=fake.build)
    await c.start()
    with pytest.raises(HarmonyError, match=f"Can't reach the Harmony hub 'Family Room' at {LIVING}"):
        await c.power_off()
    await c.stop()


async def test_start_activity_tracks_state(hub, fake):
    a = hub.catalog.activity("Watch Shield")
    assert (await hub.start_activity(a)).changed is True
    assert fake.started == [WATCH_SHIELD]
    snap = hub.snapshot()
    assert snap["current_activity"] == {"id": WATCH_SHIELD, "name": "Watch Shield"}
    assert (snap["power"], snap["transition"]) == ("on", None)


async def test_starting_the_running_activity_sends_nothing(hub, fake):
    fake.activity_id = WATCH_SHIELD
    hub.activity_id = WATCH_SHIELD
    assert (await hub.start_activity(hub.catalog.activity("Watch Shield"))).changed is False
    assert fake.started == []


async def test_power_off_is_idempotent(hub, fake):
    assert (await hub.power_off()).changed is False  # already off at start
    hub.activity_id = WATCH_SHIELD
    assert (await hub.power_off()).changed is True
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
    with pytest.raises(HarmonyError, match="Living Room is busy starting Watch Shield"):
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
    with pytest.raises(HarmonyError, match="Living Room rejected VolumeUp for Onkyo AV Receiver"):
        await hub.send(cmd)


async def test_disconnect_and_config_change(hub, fake):
    fake.push_disconnect()
    assert hub.snapshot()["reachable"] is False
    with pytest.raises(HarmonyError, match=f"Can't reach the Harmony hub 'Living Room' at {LIVING}"):
        await hub.power_off()
    fake.callbacks.config_updated({"activity": [{"id": "5", "label": "Game"}], "device": []})
    assert [a.name for a in hub.catalog.activities] == ["Game"]


async def test_stop_closes_the_connection(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    await c.stop()
    assert fake.closed and not c.available


# --- shutdown: tasks that don't stop on the first cancel ---------------------------------------
# Anything a task awaits can lose a cancellation: on Python 3.11, asyncio.wait_for returns its
# result and drops a cancel that lands in the same loop iteration (CPython PR #28149). The old
# stop() cancelled once and awaited, so such a task kept stop() waiting forever. These fakes
# drop exactly one cancel; stop() runs in its own task so a hang fails the test, not the run.
class DropsCancels:
    """Awaits "the network" in 0.2 s steps, losing the first `drops` cancellations it gets."""

    def __init__(self, drops: int = 1) -> None:
        self.drops = drops
        self.dropped = 0

    async def wait(self) -> None:
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            if self.dropped >= self.drops:
                raise
            self.dropped += 1


class UnreachableHub(FakeHarmonyAPI):
    """A hub that never answers, so the connect task retries until stopped."""

    def __init__(self, drops: int = 1) -> None:
        super().__init__()
        self.network = DropsCancels(drops)

    async def connect(self) -> bool:
        self.connect_calls += 1
        await self.network.wait()
        return False


class SlowRefresh:
    """HarmonyAPI._harmony_client, whose refresh after a reconnect takes a long time."""

    def __init__(self) -> None:
        self.network = DropsCancels()

    async def refresh_info_from_hub(self) -> None:
        while True:
            await self.network.wait()


async def stopped_within(c: HubClient, seconds: float = 3) -> None:
    stopping = asyncio.create_task(c.stop())
    done, _ = await asyncio.wait({stopping}, timeout=seconds)
    assert stopping in done, "stop() hung"


async def test_stop_ends_a_connect_task_that_drops_a_cancel():
    hub = UnreachableHub()
    c = HubClient(HubSettings(LIVING), api_factory=hub.build)
    await c.start()
    await wait_until(lambda: hub.connect_calls > 0)
    await stopped_within(c)
    assert hub.network.dropped == 1 and c._task is not None and c._task.done()


async def test_stop_ends_a_state_refresh_that_drops_a_cancel(fake):
    c = HubClient(HubSettings(LIVING), api_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    refresh = SlowRefresh()
    fake._harmony_client = refresh  # type: ignore[attr-defined]
    fake.push_disconnect()
    assert fake.callbacks is not None
    fake.callbacks.connect(LIVING)  # aioharmony reconnected on its own: _resync starts
    await asyncio.sleep(0.05)
    assert c._resync_task is not None and not c._resync_task.done()
    await stopped_within(c)
    assert refresh.network.dropped == 1 and c._resync_task.done() and fake.closed


async def test_a_task_dropping_two_cancels_cannot_hang_registry_shutdown(settings, fakes, factory):
    # HubRegistry bounds each hub's stop() with asyncio.timeout, which cancels the waiting stop(). But
    # a coroutine awaiting a task, when cancelled, passes the cancel on to that task and keeps waiting
    # for it to finish. So the old bound only held if the task honored that second cancel. Dropping
    # one cancel made shutdown sit out the whole STOP_TIMEOUT, silently (stop() suppressed the
    # CancelledError the timeout relies on); dropping two made HubRegistry.stop() never return.
    fakes[LIVING] = hub = UnreachableHub(drops=2)
    reg = HubRegistry(settings, api_factory=factory)
    await reg.start()
    await wait_until(lambda: hub.connect_calls > 0)
    stopping = asyncio.create_task(reg.stop())
    try:
        done, _ = await asyncio.wait({stopping}, timeout=3)  # well under STOP_TIMEOUT (5 s)
        assert stopping in done, "shutdown hung"
        task = reg.hubs[0]._task
        assert task is not None and task.done() and hub.network.dropped == 2
    finally:
        hub.network.drops = 0  # whatever happened, let the next cancel through so the loop can close
        stopping.cancel()
        await asyncio.wait({stopping}, timeout=1)
