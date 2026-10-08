"""Redaction, SSDP discovery, doctor and simulate: the first-contact tooling."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket

import aioharmony.hubconnector_websocket as ws_connector
import pytest

from harmony_mcp import cli
from harmony_mcp.config import load_settings
from harmony_mcp.discovery import discover, parse_reply
from harmony_mcp.doctor import render, run_doctor, to_json
from harmony_mcp.hubs import HubRegistry
from harmony_mcp.logsafe import RedactingFormatter, redact
from harmony_mcp.sim.fake_hub import FakeHub, load_fixture

from .test_contract import eventually, free_port_on
from .test_recorded import check_recorded

pytestmark = pytest.mark.anyio


# --- redaction --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("192.168.1.60: Connecting to hub", "x.x.x.60: Connecting to hub"),
        ("ws://10.0.0.7:8088/?hubId=1234", "ws://x.x.x.7:8088/?hubId=1234"),
        ("bound 127.0.0.2 and 0.0.0.0", "bound 127.0.0.2 and 0.0.0.0"),  # loopback says nothing about you
        ("mac AA:BB:CC:DD:EE:FF here", "mac xx:xx:xx:xx:EE:FF here"),
        ("topic activity/FC012C39D390/list", "topic activity/xxxxxxxxD390/list"),
        ("version 4.15.600 and id 38000001", "version 4.15.600 and id 38000001"),  # not addresses
    ],
)
def test_redact(raw, expected):
    assert redact(raw) == expected


def test_tracebacks_are_redacted_too():
    try:
        raise OSError("Connect call failed ('192.168.1.61', 8088)")
    except OSError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed for %s", ("192.168.1.61",), True)
        import sys

        record.exc_info = sys.exc_info()
    out = RedactingFormatter("%(message)s").format(record)
    assert "192.168" not in out and "x.x.x.61" in out


# --- SSDP discovery -----------------------------------------------------------------------------
HARMONY_REPLY = (
    "HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nLOCATION: http://192.0.2.50:8088/\r\n"
    "SERVER: Linux UPnP/1.0 Harmony/4.15\r\nST: urn:myharmony-com:device:harmony:1\r\n"
    "USN: uuid:hub-1::urn:myharmony-com:device:harmony:1\r\n\r\n"
)
ROUTER_REPLY = "HTTP/1.1 200 OK\r\nLOCATION: http://192.0.2.1:1900/igd.xml\r\nST: upnp:rootdevice\r\n\r\n"


class Responder(asyncio.DatagramProtocol):
    """Answers every M-SEARCH like a LAN would: a Harmony hub, a router, and some junk."""

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport
        self.searches: list[str] = []

    def datagram_received(self, data, addr):  # type: ignore[override]
        self.searches.append(data.decode())
        for reply in (HARMONY_REPLY, ROUTER_REPLY, "garbage \x00\x01"):
            self.transport.sendto(reply.encode(), addr)


async def test_ssdp_finds_the_hub_and_ignores_the_rest():
    """H-SSDP (the reply format is the generic SSDP one; the ST is the assumption)."""
    loop = asyncio.get_running_loop()
    transport, responder = await loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
    try:
        port = transport.get_extra_info("sockname")[1]
        hits = await discover(timeout=0.3, target=("127.0.0.1", port))
    finally:
        transport.close()
    assert [h.host for h in hits] == ["192.0.2.50"]  # deduped across the two M-SEARCHes
    assert "ST: urn:myharmony-com:device:harmony:1" in responder.searches[0]
    assert 'MAN: "ssdp:discover"' in responder.searches[0]


def test_reply_without_location_uses_the_sender():
    hit = parse_reply(b"HTTP/1.1 200 OK\r\nST: x\r\n\r\n", "192.0.2.9")
    assert hit is not None and hit.host == "192.0.2.9"
    assert parse_reply(b"NOTIFY * HTTP/1.1\r\n\r\n", "192.0.2.9") is None


# --- doctor -----------------------------------------------------------------------------------------
@pytest.fixture
async def fake_on_config(config_dir, monkeypatch):
    port = free_port_on("127.0.0.1")
    hub = FakeHub(load_fixture("living_room"), name="Living Room", port=port)
    await hub.start()
    (config_dir / "config.json").write_text(json.dumps({"hubs": ["127.0.0.1"], "port": port, "protocol": "WEBSOCKETS"}))
    monkeypatch.setattr(ws_connector, "DEFAULT_HUB_PORT", port)
    yield hub
    await hub.stop()


async def test_doctor_all_good_and_dump_is_redacted(fake_on_config, tmp_path):
    report = await run_doctor(load_settings(), timeout=5, dump_dir=tmp_path / "dump")
    assert report.ok, render(report)
    text = render(report)
    for line in (
        "OK   tcp",
        "OK   provision activeRemoteId 12345678",
        "OK   connect",
        "OK   catalog   2 activities, 3 devices",
    ):
        assert line in text, text
    assert "protocol assumptions not yet confirmed on hardware" in text
    dumped = (tmp_path / "dump" / "living-room.json").read_text()
    assert "someone@example.invalid" not in dumped and "<redacted>" in dumped
    assert json.loads(dumped)["config"]["activity"][1]["label"] == "Watch Shield"
    assert json.loads(to_json(report))["ok"] is True


async def test_a_dump_passes_the_recorded_fixture_checks(fake_on_config, tmp_path):
    """The loop you'll run at home: doctor --dump, then the same checks test_recorded.py runs on it."""
    await run_doctor(load_settings(), timeout=5, dump_dir=tmp_path)
    await check_recorded(tmp_path / "living-room.json")


@pytest.mark.parametrize(
    "fault,step,phrase",
    [
        ("provisioning", "provision", "not like a Harmony hub"),
        ("handshake", "connect", "websocket or a reply it waits for failed"),
    ],
)
async def test_doctor_says_which_layer_failed(fake_on_config, fault, step, phrase):
    if fault == "provisioning":
        fake_on_config.faults.provisioning = "garbage"
    else:
        fake_on_config.faults.handshake_status = 403
    report = await run_doctor(load_settings(), timeout=2)
    failed = [c for c in report.hubs[0].checks if not c.ok]
    assert [c.step for c in failed] == [step] and phrase in failed[0].hint
    assert "Some checks failed" in render(report)


async def test_doctor_with_nothing_listening(config_dir):
    port = free_port_on("127.0.0.1")
    (config_dir / "config.json").write_text(json.dumps({"hubs": ["127.0.0.1"], "port": port}))
    report = await run_doctor(load_settings(), timeout=1)
    assert report.hubs[0].checks[0].step == "tcp" and not report.ok
    assert "firewall, IoT VLAN or guest network" in report.hubs[0].checks[0].hint


# --- simulate ---------------------------------------------------------------------------------------
async def test_simulate_writes_a_config_the_server_can_use(tmp_path, monkeypatch, capsys):
    """`simulate --write-config DIR`, then the real server (MCP client, real aioharmony) against it."""
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.2", 0))
    except OSError:
        pytest.skip("no 127.0.0.2 loopback")
    port = free_port_on("127.0.0.1", "127.0.0.2")
    args = argparse.Namespace(hubs=2, port=port, step_delay=0.0, write_config=str(tmp_path), flaky=None)
    ready = asyncio.Event()
    sim = asyncio.ensure_future(cli._cmd_simulate(args, ready))
    await asyncio.wait_for(ready.wait(), 5)
    try:
        assert f"HARMONY_CONFIG_DIR={tmp_path}" in capsys.readouterr().out
        monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("HARMONY_HOSTS", raising=False)
        monkeypatch.delenv("HARMONY_HOST", raising=False)
        from mcp import Client

        from harmony_mcp import server

        async with Client(server.mcp) as c:
            await eventually(lambda: all(h.available for h in server.hubs().hubs))
            status = (await c.call_tool("get_status", {})).structured_content
            assert [h["hub"] for h in status["hubs"]] == ["Living Room", "Den"]
            r = (await c.call_tool("start_activity", {"activity": "Den/Watch TV"})).structured_content
            assert r["outcome"] == "done"
    finally:
        sim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sim


async def test_call_against_the_simulator(tmp_path, monkeypatch, capsys):
    """`call` runs one tool through the MCP layer, and its first call waits for hubs still connecting."""
    port = free_port_on("127.0.0.1")
    args = argparse.Namespace(hubs=1, port=port, step_delay=0.0, write_config=str(tmp_path), flaky=None)
    ready = asyncio.Event()
    sim = asyncio.ensure_future(cli._cmd_simulate(args, ready))
    await asyncio.wait_for(ready.wait(), 5)
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    monkeypatch.setattr(HubRegistry, "startup_grace", 5.0)
    try:
        capsys.readouterr()

        def call(*argv: str) -> argparse.Namespace:
            return cli.build_parser().parse_args(["call", *argv])

        assert await cli._cmd_call(call("get_status")) == 0  # no waiting here: the grace does it
        assert json.loads(capsys.readouterr().out)["hubs"][0]["reachable"] is True
        assert await cli._cmd_call(call("start_activity", "activity=Watch Shield")) == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "done"
        assert await cli._cmd_call(call("send_command", "command=VolumeUp", "repeat=11")) == 1
        assert "repeat" in capsys.readouterr().err  # validated like a model's call
        assert await cli._cmd_call(call("tools")) == 0
        assert "start_activity" in capsys.readouterr().out
    finally:
        sim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sim


async def test_later_calls_dont_wait_for_an_offline_hub(monkeypatch):
    from harmony_mcp.config import HubSettings, Settings

    reg = HubRegistry(Settings(hubs=(HubSettings("192.0.2.9"),)))  # never started: never tries
    monkeypatch.setattr(HubRegistry, "startup_grace", 0.2)
    loop = asyncio.get_running_loop()
    t = loop.time()
    await reg.ready()  # the first call waits out the grace...
    assert 0.15 < loop.time() - t < 1
    t = loop.time()
    await reg.ready()  # ...later ones don't
    assert loop.time() - t < 0.05
