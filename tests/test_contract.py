"""Contract tests: the *real* aioharmony library, driven by our HubClient, against the wire-level fake hub.

The rest of the suite fakes HarmonyAPI in Python. These tests cross a real
socket instead, so they check that our calls produce the frames the hub
protocol expects, and that the replies aioharmony waits for are the ones the
fake hub sends. Each test names the assumption (assumptions.py) it documents.

Timing is real here (aioharmony's own reconnect waits 1 s), so a few tests
take a second or two.
"""

from __future__ import annotations

import asyncio
import copy
import socket
from collections.abc import Callable

import aioharmony.hubconnector_websocket as ws_connector
import pytest

from harmony_mcp.client import HarmonyError, HubClient
from harmony_mcp.config import HubSettings
from harmony_mcp.sim.fake_hub import CMD_PROVISION, FakeHub, load_fixture

pytestmark = pytest.mark.anyio

WATCH_SHIELD = 38000001
ONKYO = 71000001


async def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait (in real time) until condition() holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


def free_port_on(*hosts: str) -> int:
    """A port that is free on every host (so two fake hubs can share it on 127.0.0.1 and 127.0.0.2)."""
    for _ in range(20):
        with socket.socket() as s:
            s.bind((hosts[0], 0))
            port = s.getsockname()[1]
        try:
            for h in hosts[1:]:
                with socket.socket() as s:
                    s.bind((h, port))
            return port
        except OSError:
            continue
    raise RuntimeError("no common free port")


@pytest.fixture(autouse=True)
def real_timing(monkeypatch):
    # conftest's `fast` fixture shrinks these for the in-process fakes; over a
    # real socket give the protocol room to breathe.
    monkeypatch.setattr(HubClient, "activity_timeout", 5.0)
    monkeypatch.setattr(HubClient, "retry_delay", 0.05)


@pytest.fixture
async def fake_hub(monkeypatch):
    port = free_port_on("127.0.0.1")
    hub = FakeHub(load_fixture("living_room"), name="Living Room", port=port)
    await hub.start()
    # ASSUMPTION H-PORT: aioharmony always uses one module-level port; point it at the fake.
    monkeypatch.setattr(ws_connector, "DEFAULT_HUB_PORT", port)
    yield hub
    await hub.stop()


@pytest.fixture
async def client(fake_hub):
    c = HubClient(HubSettings("127.0.0.1"), protocol="WEBSOCKETS")
    await c.start()
    await eventually(lambda: c.available)
    yield c
    await c.stop()


# --- connecting ----------------------------------------------------------------------
async def test_connect_reads_name_firmware_config_and_state(client, fake_hub):
    """H-PROVISION, H-FRAMES, H-CONFIG-SHAPE, H-NAME"""
    snap = client.snapshot()
    assert (snap["hub"], snap["firmware"], snap["power"]) == ("Living Room", "4.15.600", "off")
    assert [a.name for a in client.catalog.activities] == ["Watch Shield", "Listen to Music"]
    assert {
        CMD_PROVISION,
        "vnd.logitech.connect/vnd.logitech.statedigest?get",
        "vnd.logitech.harmony/vnd.logitech.harmony.engine?config",
        "connect.discoveryinfo?get",
        "vnd.logitech.harmony/vnd.logitech.harmony.engine?getCurrentActivity",
    } <= set(fake_hub.received)


# --- activities -------------------------------------------------------------------------
async def test_start_activity_round_trip(client, fake_hub):
    """H-START-SEQUENCE: the frames aioharmony waits for arrive in an order it accepts."""
    fake_hub.step_delay = 0.05
    assert (await client.start_activity(client.catalog.activity("Watch Shield"))).changed
    assert fake_hub.current_activity == WATCH_SHIELD and fake_hub.started == [WATCH_SHIELD]
    snap = client.snapshot()
    assert snap["current_activity"] == {"id": WATCH_SHIELD, "name": "Watch Shield"} and snap["transition"] is None


async def test_power_off_round_trip(client, fake_hub):
    """H-POWEROFF"""
    await client.start_activity(client.catalog.activity("Watch Shield"))
    assert (await client.power_off()).changed
    assert fake_hub.started == [WATCH_SHIELD, -1] and client.snapshot()["power"] == "off"


async def test_transition_is_visible_and_blocks_presses(client, fake_hub):
    fake_hub.step_delay = 0.3
    start = asyncio.ensure_future(client.start_activity(client.catalog.activity("Watch Shield")))
    await eventually(lambda: client.snapshot()["transition"] == "starting Watch Shield")
    cmd = client.catalog.find_command(client.catalog.device("Onkyo AV Receiver").commands, "VolumeUp")
    with pytest.raises(HarmonyError, match="busy starting Watch Shield"):
        await client.send(cmd)
    await start
    assert fake_hub.presses == []


async def test_refused_activity_reports_the_hubs_message(client, fake_hub):
    fake_hub.faults.refuse_activity = "Device not responding"
    with pytest.raises(HarmonyError, match="Device not responding"):
        await client.start_activity(client.catalog.activity("Watch Shield"))


async def test_activity_that_never_finishes_times_out(client, fake_hub, monkeypatch):
    """H-TIMING: our timeout fires and says to check status, rather than hanging the tool call."""
    monkeypatch.setattr(HubClient, "activity_timeout", 0.5)
    fake_hub.faults.never_finish = True
    with pytest.raises(HarmonyError, match="didn't finish"):
        await client.start_activity(client.catalog.activity("Watch Shield"))


# --- presses ----------------------------------------------------------------------------
async def test_press_is_press_then_release_with_pauses(client, fake_hub, monkeypatch):
    """H-PRESS-SILENT: success is silence; aioharmony sends press and release frames."""
    monkeypatch.setattr(HubClient, "repeat_gap", 0.1)
    cmd = client.catalog.find_command(client.catalog.device("Onkyo AV Receiver").commands, "VolumeUp")
    await client.send(cmd, repeat=2)
    await eventually(lambda: len(fake_hub.presses) == 4)
    assert [(p.device_id, p.command, p.status) for p in fake_hub.presses] == [
        (ONKYO, "VolumeUp", "press"),
        (ONKYO, "VolumeUp", "release"),
        (ONKYO, "VolumeUp", "press"),
        (ONKYO, "VolumeUp", "release"),
    ]
    gap = fake_hub.presses[2].at - fake_hub.presses[1].at
    assert gap >= 0.09  # the bare-float pause between repeats reached the wire


async def test_rejected_press_surfaces_the_hub_error(client, fake_hub):
    """H-PRESS-ERROR: a non-200 reply becomes an actionable HarmonyError."""
    cmd = client.catalog.find_command(client.catalog.device("Onkyo AV Receiver").commands, "VolumeUp")
    fake_hub.config = copy.deepcopy(fake_hub.config)
    for d in fake_hub.config["device"]:
        d["controlGroup"] = []  # the hub "forgot" every command
    with pytest.raises(HarmonyError, match=r"rejected VolumeUp for Onkyo AV Receiver: Unknown command \(code 417\)"):
        await client.send(cmd)


# --- connection trouble -------------------------------------------------------------------
async def test_reconnects_after_the_hub_drops_the_socket(client, fake_hub):
    """H-RECONNECT: aioharmony reconnects by itself and our state follows its callbacks."""
    await fake_hub.drop_connections()
    await eventually(lambda: not client.available, timeout=2)
    await eventually(lambda: client.available, timeout=5)
    assert fake_hub.handshakes == 2
    await client.start_activity(client.catalog.activity("Listen to Music"))


async def test_recovers_from_a_malformed_frame(client, fake_hub, monkeypatch):
    """A garbled frame mid-start: what really happens, end to end.

    aioharmony's listener dies on the bad JSON and reconnects, so the start's
    remaining replies go to a dead socket and our call times out with advice.
    The hub carried on and *is* on Watch Shield; after the reconnect we
    re-read its state (HubClient._resync), so get_status tells the truth and
    the next command works.
    """
    monkeypatch.setattr(HubClient, "activity_timeout", 1.0)
    fake_hub.faults.malformed_next = 1
    with pytest.raises(HarmonyError, match="check get_status"):
        await client.start_activity(client.catalog.activity("Watch Shield"))
    await eventually(lambda: fake_hub.handshakes == 2 and client.available, timeout=6)
    await eventually(lambda: client.snapshot()["current_activity"] == {"id": WATCH_SHIELD, "name": "Watch Shield"})
    assert (await client.power_off()).changed


async def test_slow_replies_still_work(client, fake_hub):
    fake_hub.faults.reply_delay = 0.3
    assert (await client.start_activity(client.catalog.activity("Watch Shield"))).changed


@pytest.mark.parametrize("fault", ["error", "no_remote_id", "garbage"])
async def test_provisioning_failures_keep_retrying_until_fixed(fake_hub, fault):
    """H-PROVISION: without an activeRemoteId there is no websocket; we keep retrying with backoff."""
    fake_hub.faults.provisioning = fault
    c = HubClient(HubSettings("127.0.0.1"), protocol="WEBSOCKETS")
    await c.start()
    try:
        await eventually(lambda: fake_hub.received.count(CMD_PROVISION) >= 2)
        assert not c.available and c.snapshot()["reachable"] is False
        fake_hub.faults.provisioning = "ok"
        await eventually(lambda: c.available)
    finally:
        await c.stop()


async def test_refused_handshake_keeps_retrying(fake_hub):
    fake_hub.faults.handshake_status = 403
    c = HubClient(HubSettings("127.0.0.1"), protocol="WEBSOCKETS")
    await c.start()
    try:
        await eventually(lambda: fake_hub.received.count(CMD_PROVISION) >= 2)
        assert fake_hub.handshakes == 0 and not c.available
        fake_hub.faults.handshake_status = None
        await eventually(lambda: c.available)
    finally:
        await c.stop()


async def test_unreachable_hub_never_blocks_startup(monkeypatch):
    port = free_port_on("127.0.0.1")  # nothing listens here
    monkeypatch.setattr(ws_connector, "DEFAULT_HUB_PORT", port)
    c = HubClient(HubSettings("127.0.0.1", "Garage"), protocol="WEBSOCKETS")
    await c.start()  # returns at once; connecting happens in the background
    await asyncio.sleep(0.2)
    assert c.snapshot()["reachable"] is False
    with pytest.raises(HarmonyError, match="Can't reach the Harmony hub 'Garage'"):
        await c.power_off()
    await c.stop()


async def test_config_pushed_from_the_app_reloads_the_catalog(client, fake_hub):
    """H-CONFIG-PUSH"""
    new = copy.deepcopy(fake_hub.config)
    new["activity"].append({"id": "38000099", "label": "Play Switch", "controlGroup": []})
    await fake_hub.push_config_change(new)
    await eventually(lambda: client.catalog.activity("Play Switch") is not None)


# --- two hubs, end to end through MCP ----------------------------------------------------
@pytest.fixture
async def two_fake_hubs(monkeypatch, tmp_path):
    """Two wire-level fakes on 127.0.0.1 and 127.0.0.2 (same port, as aioharmony needs)."""
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.2", 0))
    except OSError:
        pytest.skip("no 127.0.0.2 loopback (macOS: sudo ifconfig lo0 alias 127.0.0.2)")
    port = free_port_on("127.0.0.1", "127.0.0.2")
    living = FakeHub(load_fixture("living_room"), name="Living Room", host="127.0.0.1", port=port)
    den = FakeHub(load_fixture("den"), name="Den", host="127.0.0.2", port=port, remote_id=87654321)
    for hub in (living, den):
        await hub.start()
    monkeypatch.setattr(ws_connector, "DEFAULT_HUB_PORT", port)
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    (tmp_path / "config.json").write_text(
        '{"protocol": "WEBSOCKETS", "hubs": [{"host": "127.0.0.1"}, {"host": "127.0.0.2"}]}'
    )
    yield living, den
    for hub in (living, den):
        await hub.stop()


async def test_two_hubs_end_to_end_through_mcp(two_fake_hubs):
    """MCP client -> tools -> HubRegistry -> real aioharmony -> two fake hubs, names from the hubs themselves."""
    from mcp import Client

    from harmony_mcp import server

    living, den = two_fake_hubs
    async with Client(server.mcp) as c:
        await eventually(lambda: all(h.available for h in server.hubs().hubs))
        hubs = (await c.call_tool("get_status", {})).structured_content["hubs"]
        assert [h["hub"] for h in hubs] == ["Living Room", "Den"]  # friendlyName over the wire

        result = await c.call_tool("start_activity", {"activity": "Listen to Music"})
        assert result.is_error and "exists on Living Room, Den" in result.content[0].text
        assert living.started == den.started == []

        assert not (await c.call_tool("start_activity", {"activity": "Listen to Music", "hub": "Den"})).is_error
        assert den.current_activity == 39000002 and living.current_activity == -1

        await c.call_tool("send_command", {"command": "volume up"})  # only the Den is on
        await eventually(lambda: [p.command for p in den.presses] == ["VolumeUp", "VolumeUp"])
        assert living.presses == []

        await c.call_tool("start_activity", {"activity": "Watch Shield"})
        assert (await c.call_tool("power_off", {})).is_error  # both on: ask, don't guess
        assert not (await c.call_tool("power_off", {"hub": "all"})).is_error
        assert living.current_activity == den.current_activity == -1
