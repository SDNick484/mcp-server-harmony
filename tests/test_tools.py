"""MCP tools, called through a real MCP client connected in-process.

This tests the contract the model actually sees: tools/list (names, titles,
annotations, input and output schemas), argument validation, and results.
"""

from __future__ import annotations

import pytest
from mcp import Client

from harmony_mcp import server
from harmony_mcp.client import HubClient

from .conftest import ONKYO, SHIELD, WATCH_SHIELD, wait_until

pytestmark = pytest.mark.anyio

TOOL_NAMES = {
    "get_status",
    "list_activities",
    "list_devices",
    "list_commands",
    "start_activity",
    "power_off",
    "send_command",
}


@pytest.fixture
async def mcp_client(settings, fake, monkeypatch):
    # The lifespan builds the HubClient; swap in one that uses the fake hub.
    monkeypatch.setattr(server, "HubClient", lambda s: HubClient(s, api_factory=fake.build))
    async with Client(server.mcp) as c:
        await wait_until(lambda: server.client().available)
        yield c


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


def text(result) -> str:
    return result.content[0].text


# --- tools/list --------------------------------------------------------------
async def test_tool_names(mcp_client):
    assert set(await tools(mcp_client)) == TOOL_NAMES


async def test_every_tool_has_title_description_and_annotations(mcp_client):
    # Without annotations a client must assume the worst. State them all.
    for t in (await tools(mcp_client)).values():
        assert t.title and t.description, t.name
        a = t.annotations
        assert a is not None and a.read_only_hint is not None and a.open_world_hint is False, t.name
        if not a.read_only_hint:
            assert a.destructive_hint is False and a.idempotent_hint is not None, t.name


async def test_annotations_match_behavior(mcp_client):
    t = await tools(mcp_client)
    for name in ("get_status", "list_activities", "list_devices", "list_commands"):
        assert t[name].annotations.read_only_hint is True, name
    assert t["start_activity"].annotations.idempotent_hint is True  # an activity is a state
    assert t["power_off"].annotations.idempotent_hint is True
    assert t["send_command"].annotations.idempotent_hint is False  # VolumeUp twice is +2


async def test_send_command_schema(mcp_client):
    schema = (await tools(mcp_client))["send_command"].input_schema
    props = schema["properties"]
    assert schema["required"] == ["command"]
    assert (props["repeat"]["minimum"], props["repeat"]["maximum"], props["repeat"]["default"]) == (1, 10, 1)


async def test_get_status_publishes_output_schema(mcp_client):
    schema = (await tools(mcp_client))["get_status"].output_schema
    assert set(schema["required"]) == {
        "host",
        "reachable",
        "hub_name",
        "firmware",
        "power",
        "current_activity",
        "transition",
    }


# --- tools/call --------------------------------------------------------------
async def test_get_status(mcp_client):
    result = await mcp_client.call_tool("get_status", {})
    assert result.structured_content == {
        "host": "192.0.2.20",
        "reachable": True,
        "hub_name": "Living Room Hub",
        "firmware": "4.15.600",
        "power": "off",
        "current_activity": None,
        "transition": None,
    }


async def test_list_activities_marks_the_running_one(mcp_client):
    await mcp_client.call_tool("start_activity", {"activity": "Watch Shield"})
    rows = (await mcp_client.call_tool("list_activities", {})).structured_content["result"]
    assert {r["name"]: r["running"] for r in rows} == {"Watch Shield": True, "Listen to Music": False}


async def test_list_commands_defaults_to_the_running_activity(mcp_client):
    result = await mcp_client.call_tool("list_commands", {})
    assert result.is_error and "No activity is running" in text(result)
    await mcp_client.call_tool("start_activity", {"activity": "watch shield"})
    rows = (await mcp_client.call_tool("list_commands", {})).structured_content["result"]
    assert {r["name"]: r["device"] for r in rows}["VolumeUp"] == "Onkyo AV Receiver"


async def test_start_activity_twice(mcp_client, fake):
    assert text(await mcp_client.call_tool("start_activity", {"activity": "Watch Shield"})) == "Started Watch Shield"
    assert text(await mcp_client.call_tool("start_activity", {"activity": "Watch Shield"})) == (
        "Watch Shield was already running"
    )
    assert fake.started == [WATCH_SHIELD]


async def test_unknown_activity_lists_known_ones(mcp_client, fake):
    result = await mcp_client.call_tool("start_activity", {"activity": "Watch Netflix"})
    assert result.is_error
    assert "Unknown activity 'Watch Netflix'. Activities: Watch Shield, Listen to Music" in text(result)
    assert fake.started == []


async def test_send_command_routes_through_the_activity(mcp_client, fake):
    # The model says "volume up"; the activity knows volume lives on the receiver.
    await mcp_client.call_tool("start_activity", {"activity": "Watch Shield"})
    result = await mcp_client.call_tool("send_command", {"command": "volume up", "repeat": 2})
    assert text(result) == "Sent VolumeUp x2 to Onkyo AV Receiver"
    await mcp_client.call_tool("send_command", {"command": "Pause"})
    assert fake.presses() == [(ONKYO, "VolumeUp"), (ONKYO, "VolumeUp"), (SHIELD, "Pause")]


async def test_send_command_to_a_device_works_while_off(mcp_client, fake):
    result = await mcp_client.call_tool("send_command", {"command": "PowerOn", "device": "Onkyo AV Receiver"})
    assert not result.is_error
    assert fake.presses() == [(ONKYO, "PowerOn")]


@pytest.mark.parametrize(
    "args",
    [
        {"command": "VolumeUp", "device": "Onkyo AV Receiver", "repeat": 11},
        {"command": "VolumeUp", "device": "Onkyo AV Receiver", "repeat": 0},
        {"command": "SelfDestruct", "device": "Onkyo AV Receiver"},
        {"command": "VolumeUp", "device": "Xbox"},
        {"command": "VolumeUp"},  # nothing running and no device
    ],
)
async def test_bad_sends_never_reach_the_hub(mcp_client, fake, args):
    result = await mcp_client.call_tool("send_command", args)
    assert result.is_error
    assert fake.sent == []


async def test_a_command_from_another_device_is_not_a_back_door(mcp_client, fake):
    # PowerToggle exists, but on the TV: naming the Shield must not find it.
    result = await mcp_client.call_tool("send_command", {"command": "PowerToggle", "device": "NVIDIA Shield"})
    assert result.is_error and "has no command 'PowerToggle'" in text(result)
    assert fake.sent == []


async def test_unreachable_hub_explains_itself(mcp_client, fake):
    fake.push_disconnect()
    result = await mcp_client.call_tool("power_off", {})
    assert result.is_error and "Can't reach the Harmony hub" in text(result)


async def test_unconfigured_server_still_answers_status(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    monkeypatch.setattr(server, "HubClient", lambda s: HubClient(s, api_factory=fake.build))
    async with Client(server.mcp) as c:
        status = (await c.call_tool("get_status", {})).structured_content
        assert (status["host"], status["reachable"]) == (None, False)
        result = await c.call_tool("power_off", {})
        assert result.is_error and "HARMONY_HOST" in text(result)
