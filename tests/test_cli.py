"""`check`: first contact with a hub, and how it fills in config.json."""

from __future__ import annotations

import json

import pytest

from harmony_mcp import cli

from .conftest import DEN, LIVING

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake_api(fakes, monkeypatch):
    # cli builds HarmonyAPI(ip_address=..., protocol=...) with no callbacks.
    monkeypatch.setattr(
        cli,
        "HarmonyAPI",
        lambda ip_address, protocol: fakes[ip_address].build(ip_address=ip_address, protocol=protocol, callbacks=None),
    )


async def test_check_adds_a_hub_with_its_own_name(tmp_path, monkeypatch, fake_api, capsys):
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    assert await cli._cmd_check(LIVING) == 0
    assert await cli._cmd_check(DEN) == 0
    out = capsys.readouterr().out
    assert "Living Room (firmware" in out and "Watch Shield" in out and "Den TV" in out
    assert json.loads((tmp_path / "config.json").read_text())["hubs"] == [
        {"host": LIVING, "name": "Living Room"},
        {"host": DEN, "name": "Den"},
    ]


async def test_check_without_host_checks_every_hub(two_hubs, fake_api, fakes):
    fakes[DEN].connect_results = [False]
    assert await cli._cmd_check(None) == 1  # the Den didn't answer
    assert fakes[LIVING].closed  # connections are closed after checking
    # The Living Room's name was missing and is now filled in; the Den's is untouched.
    hubs = json.loads((two_hubs / "config.json").read_text())["hubs"]
    assert hubs == [{"host": LIVING, "name": "Living Room"}, {"host": DEN, "name": "Den"}]


async def test_check_with_nothing_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    assert await cli._cmd_check(None) == 2
    assert "once per hub" in capsys.readouterr().err


async def test_call_tools_lists_namespaced_tools(capsys):
    import argparse

    ret = await cli._cmd_call(argparse.Namespace(tool="tools", args=[]))
    assert ret == 0
    out = capsys.readouterr().out
    assert "harmony_get_status" in out
    assert "harmony_start_activity" in out


async def test_call_shorthand_and_full_name(two_hubs, factory, monkeypatch, capsys):
    import argparse

    from harmony_mcp import server
    from harmony_mcp.hubs import HubRegistry

    monkeypatch.setattr(server, "HubRegistry", lambda s: HubRegistry(s, api_factory=factory))
    # Unprefixed shorthand
    ret = await cli._cmd_call(argparse.Namespace(tool="get_status", args=[]))
    assert ret == 0
    out = capsys.readouterr().out
    assert "Living Room" in out
    assert "x.x.x.20" in out

    # Full prefixed name
    ret = await cli._cmd_call(argparse.Namespace(tool="harmony_get_status", args=[]))
    assert ret == 0
    out = capsys.readouterr().out
    assert "Living Room" in out
    assert "x.x.x.20" in out
