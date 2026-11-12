"""`export` and the worksheet: a hub's setup written to files the owner keeps.

The CLI tests run the real aioharmony against the wire-level fake hub, the way the
doctor tests in test_tooling.py do, so the config in the files is what came over the socket.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime

import aioharmony.hubconnector_websocket as ws_connector
import pytest

from harmony_mcp import cli
from harmony_mcp.catalog import Catalog
from harmony_mcp.export import NOT_A_BACKUP, worksheet
from harmony_mcp.sim.fake_hub import FakeHub, load_fixture

from .conftest import DEN_CONFIG, SAMPLE_CONFIG
from .test_contract import free_port_on

pytestmark = pytest.mark.anyio

# Account identifiers planted in the living_room fixture (global, content) and served by the fake hub's
# provisioning reply. None may reach either file.
ACCOUNT_BITS = ("owner@example.invalid", "ownerhouseholdid", "someone@example.invalid", "USMS")


@pytest.fixture
async def fake_hub(config_dir, monkeypatch):
    port = free_port_on("127.0.0.1")
    hub = FakeHub(load_fixture("living_room"), name="Living Room", port=port)
    await hub.start()
    (config_dir / "config.json").write_text(json.dumps({"hubs": ["127.0.0.1"], "port": port, "protocol": "WEBSOCKETS"}))
    monkeypatch.setattr(ws_connector, "DEFAULT_HUB_PORT", ws_connector.DEFAULT_HUB_PORT)  # restored afterwards
    yield hub
    await hub.stop()


def export_args(*argv: str):
    return cli.build_parser().parse_args(["export", *argv])


async def test_export_writes_the_config_and_a_worksheet(fake_hub, config_dir, capsys):
    assert await cli._cmd_export(export_args()) == 0
    out_dir = config_dir / "export"  # the default: under the config folder
    out = capsys.readouterr().out
    assert str(out_dir / "living-room.json") in out and "not backups" in out

    doc = json.loads((out_dir / "living-room.json").read_text())
    assert doc["_meta"]["hub"] == "Living Room" and doc["_meta"]["host"] == "127.0.0.1"
    # The full config as the hub returned it, fixit included.
    assert [a["label"] for a in doc["config"]["activity"]] == ["PowerOff", "Watch Shield", "Listen to Music"]
    assert doc["config"]["activity"][1]["fixit"]["71000001"]["Input"] == "STRM BOX"
    assert Catalog.from_config(doc["config"]) == Catalog.from_config(SAMPLE_CONFIG)

    md = (out_dir / "living-room.md").read_text()
    assert md.startswith("# Living Room: Harmony setup worksheet")
    assert NOT_A_BACKUP in md
    assert "| Onkyo AV Receiver | on | STRM BOX |" in md
    assert "| Volume | Onkyo AV Receiver | VolumeUp, Mute |" in md
    assert "### Living Room TV (LG OLED65)" in md and "| Power | PowerToggle |" in md


async def test_account_identifiers_never_reach_either_file(fake_hub, tmp_path):
    assert await cli._cmd_export(export_args("--dir", str(tmp_path / "out"))) == 0
    for name in ("living-room.json", "living-room.md"):
        text = (tmp_path / "out" / name).read_text()
        for bit in ACCOUNT_BITS:
            assert bit not in text, (name, bit)
    doc = json.loads((tmp_path / "out" / "living-room.json").read_text())
    assert doc["config"]["global"] == {"locale": "en-US", "timeStampHash": "<redacted>"}
    assert doc["config"]["content"]["householdUserProfileUri"] == "<redacted>"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
async def test_files_are_readable_by_the_owner_only(fake_hub, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "living-room.md").write_text("an old export, world-readable")
    (out / "living-room.md").chmod(0o644)
    assert await cli._cmd_export(export_args("--dir", str(out))) == 0
    for name in ("living-room.json", "living-room.md"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600, name
    assert "an old export" not in (out / "living-room.md").read_text()


async def test_addresses_stay_unless_redact(fake_hub, tmp_path, monkeypatch):
    # The fake hub is on loopback, which redaction leaves alone, so give it a LAN-looking address in a device.
    fake_hub.config["device"][0]["ipAddress"] = "192.168.1.77"
    assert await cli._cmd_export(export_args("--dir", str(tmp_path / "a"))) == 0
    assert "192.168.1.77" in (tmp_path / "a" / "living-room.json").read_text()
    assert await cli._cmd_export(export_args("--dir", str(tmp_path / "b"), "--redact")) == 0
    shared = (tmp_path / "b" / "living-room.json").read_text()
    assert "192.168.1.77" not in shared and "x.x.x.77" in shared


async def test_export_of_an_unreachable_hub_fails_cleanly(config_dir, tmp_path, capsys):
    port = free_port_on("127.0.0.1")
    (config_dir / "config.json").write_text(json.dumps({"hubs": ["127.0.0.1"], "port": port}))
    assert await cli._cmd_export(export_args("--dir", str(tmp_path / "out"), "--timeout", "2")) == 1
    assert "couldn't connect" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


async def test_export_with_nothing_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    assert await cli._cmd_export(export_args()) == 2
    assert "check --host" in capsys.readouterr().err


# --- the worksheet itself (pure) --------------------------------------------------------------
WHEN = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)


def test_worksheet_says_unknown_instead_of_guessing():
    # The Den's "Listen to Music" has no fixit.
    md = worksheet("Den", Catalog.from_config(DEN_CONFIG), source="'Den' at 192.0.2.21", when=WHEN)
    music = md.split("### Listen to Music", 1)[1].split("###", 1)[0]
    assert "Expected state: **unknown**" in music and "| Den TV | unknown |" in music
    watch = md.split("### Watch TV", 1)[1].split("###", 1)[0]
    assert "unknown" not in watch and "| Den TV | on | HDMI 1 |" in watch


def test_worksheet_cells_cant_break_the_table():
    config = {
        "activity": [{"id": "1", "label": "TV | Games", "controlGroup": []}],
        "device": [{"id": "2", "label": "Box|One", "manufacturer": "A\nB", "model": ""}],
    }
    md = worksheet("Hub", Catalog.from_config(config), source="x", when=WHEN)
    assert "| Box\\|One | A B |   | 0 |" in md
    assert "### TV | Games" in md  # headings aren't tables
    assert "_No commands._" in md
