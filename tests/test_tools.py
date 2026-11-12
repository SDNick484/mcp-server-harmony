"""MCP tools, called through a real MCP client connected in-process (one hub).

This tests the contract the model actually sees: tools/list (names, titles,
annotations, input and output schemas), argument validation, and results.
test_multi_hub.py covers what changes with two hubs.
"""

from __future__ import annotations

import httpx2
import pytest
from mcp import Client

from harmony_mcp import cli, remote, server
from harmony_mcp.hubs import HubRegistry

from .conftest import ONKYO, SHIELD, WATCH_SHIELD, wait_until

pytestmark = pytest.mark.anyio

TOOL_NAMES = {
    "harmony_get_status",
    "harmony_list_activities",
    "harmony_list_devices",
    "harmony_list_commands",
    "harmony_start_activity",
    "harmony_power_off",
    "harmony_send_command",
    "harmony_describe_activity",
    "harmony_set_sleep_timer",
    "harmony_tune_channel",
}


async def connect(monkeypatch, factory):
    """An in-process MCP client whose server talks to the fake hubs."""
    # The lifespan builds the HubRegistry; swap in one that uses the fakes.
    monkeypatch.setattr(server, "HubRegistry", lambda s: HubRegistry(s, api_factory=factory))
    c = Client(server.mcp)
    await c.__aenter__()
    await wait_until(lambda: all(h.available for h in server.hubs().hubs))
    return c


@pytest.fixture
async def mcp_client(settings, factory, monkeypatch):
    c = await connect(monkeypatch, factory)
    yield c
    await c.__aexit__(None, None, None)


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


def text(result) -> str:
    return result.content[0].text


def detail(result) -> str:
    """The one-sentence detail of a write tool's structured result."""
    assert not result.is_error, text(result)
    return result.structured_content["detail"]


def outcomes(result) -> list[tuple[str, str]]:
    """(hub, outcome) per hub from power_off."""
    assert not result.is_error, text(result)
    return [(r["hub"], r["outcome"]) for r in result.structured_content["results"]]


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
    for name in (
        "harmony_get_status",
        "harmony_list_activities",
        "harmony_list_devices",
        "harmony_list_commands",
        "harmony_describe_activity",
    ):
        assert t[name].annotations.read_only_hint is True, name
    assert t["harmony_start_activity"].annotations.idempotent_hint is True  # an activity is a state
    assert t["harmony_power_off"].annotations.idempotent_hint is True
    assert t["harmony_send_command"].annotations.idempotent_hint is False  # VolumeUp twice is +2


async def test_every_tool_but_status_takes_an_optional_hub(mcp_client):
    for name, t in (await tools(mcp_client)).items():
        props = t.input_schema.get("properties", {})
        if name == "harmony_get_status":
            assert not props
        else:
            assert "hub" in props and "hub" not in t.input_schema.get("required", []), name


async def test_send_command_schema(mcp_client):
    schema = (await tools(mcp_client))["harmony_send_command"].input_schema
    props = schema["properties"]
    assert schema["required"] == ["command"]
    assert (props["repeat"]["minimum"], props["repeat"]["maximum"], props["repeat"]["default"]) == (1, 10, 1)


async def test_get_status_publishes_output_schema(mcp_client):
    schema = (await tools(mcp_client))["harmony_get_status"].output_schema
    hub = schema["$defs"]["Status"]
    assert set(hub["required"]) == {
        "hub",
        "host",
        "reachable",
        "firmware",
        "power",
        "current_activity",
        "transition",
        "sleep_timer",
        "last_sleep_timer",
    }


# --- tools/call --------------------------------------------------------------
async def test_initialize_reports_the_package_version(mcp_client):
    from harmony_mcp import __version__

    assert mcp_client.server_info.version == __version__


async def test_get_status(mcp_client):
    result = await mcp_client.call_tool("harmony_get_status", {})
    assert result.structured_content == {
        "dry_run": False,
        "hubs": [
            {
                "hub": "Living Room",
                "host": "x.x.x.20",
                "reachable": True,
                "firmware": "4.15.600",
                "power": "off",
                "current_activity": None,
                "transition": None,
                "sleep_timer": None,
                "last_sleep_timer": None,
            }
        ],
    }


async def test_list_activities_marks_the_running_one(mcp_client):
    await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Shield"})
    rows = (await mcp_client.call_tool("harmony_list_activities", {})).structured_content["result"]
    assert {r["name"]: r["running"] for r in rows} == {"Watch Shield": True, "Listen to Music": False}
    assert {r["hub"] for r in rows} == {"Living Room"}


async def test_list_commands_defaults_to_the_running_activity(mcp_client):
    result = await mcp_client.call_tool("harmony_list_commands", {})
    assert result.is_error and "No activity is running" in text(result)
    await mcp_client.call_tool("harmony_start_activity", {"activity": "watch shield"})
    listing = (await mcp_client.call_tool("harmony_list_commands", {})).structured_content
    assert (listing["hub"], listing["source"]) == ("Living Room", "activity Watch Shield")
    assert {r["name"]: r["device"] for r in listing["commands"]}["VolumeUp"] == "Onkyo AV Receiver"


async def test_start_activity_twice(mcp_client, fake):
    result = await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Shield"})
    assert detail(result) == "Start Watch Shield on Living Room"
    result = await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Shield"})
    assert detail(result) == "Watch Shield was already running on Living Room"
    assert fake.started == [WATCH_SHIELD]


async def test_unknown_activity_lists_known_ones(mcp_client, fake):
    result = await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Netflix"})
    assert result.is_error
    assert "Unknown activity 'Watch Netflix'. Living Room: Watch Shield, Listen to Music." in text(result)
    assert fake.started == []


async def test_power_off(mcp_client, fake):
    assert outcomes(await mcp_client.call_tool("harmony_power_off", {})) == []
    await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Shield"})
    assert outcomes(await mcp_client.call_tool("harmony_power_off", {})) == [("Living Room", "done")]
    assert fake.started == [WATCH_SHIELD, -1]


async def test_power_off_force_breaks_a_stuck_start(mcp_client, fake):
    fake.push_starting(WATCH_SHIELD)  # a start from the physical remote, stuck warming up
    busy = await mcp_client.call_tool("harmony_power_off", {})
    assert busy.is_error and "busy starting Watch Shield" in text(busy)
    assert outcomes(await mcp_client.call_tool("harmony_power_off", {"force": True})) == [("Living Room", "done")]
    assert fake.started == [-1]
    status = server.hubs().hubs[0].snapshot()
    assert status["power"] == "off" and status["transition"] is None


async def test_power_off_force_resends_during_a_power_off(mcp_client, fake):
    # Already off, and a power-off sequence is "running": without force that's busy, and when idle it's a
    # no-op. With force the power off goes out regardless.
    fake.push_starting(-1)
    busy = await mcp_client.call_tool("harmony_power_off", {})
    assert busy.is_error and "busy powering off" in text(busy)
    assert outcomes(await mcp_client.call_tool("harmony_power_off", {"force": True})) == [("Living Room", "done")]
    assert fake.started == [-1]
    assert server.hubs().hubs[0].starting_id is None


async def test_power_off_force_on_a_hub_that_looks_off_needs_its_name(mcp_client, fake):
    # Our state can be wrong (say the TV came on by itself). Forcing then sends the power off anyway, but
    # only to a named hub: with no hub, power_off acts on hubs that are on or mid-sequence, never a guess.
    assert outcomes(await mcp_client.call_tool("harmony_power_off", {"force": True})) == []
    named = await mcp_client.call_tool("harmony_power_off", {"hub": "Living Room", "force": True})
    assert outcomes(named) == [("Living Room", "done")]
    assert fake.started == [-1]


async def test_describe_activity(mcp_client, fake):
    result = await mcp_client.call_tool("harmony_describe_activity", {"activity": "watch shield"})
    assert not result.is_error, text(result)
    d = result.structured_content
    assert (d["hub"], d["name"], d["ref"], d["expected_state"]) == (
        "Living Room",
        "Watch Shield",
        "Living Room/Watch Shield",
        "hub",
    )
    assert [(x["name"], x["power"], x["input"]) for x in d["devices"]] == [
        ("Onkyo AV Receiver", "on", "STRM BOX"),
        ("NVIDIA Shield", "on", None),
        ("Living Room TV", "on", "HDMI 1"),
    ]
    assert d["devices"][0]["manufacturer"] == "Onkyo" and d["devices"][0]["model"] == "TX-NR7100"
    assert "toggle" in d["devices"][2]["notes"][0]
    assert [(b["group"], b["devices"]) for b in d["buttons"]] == [
        ("Volume", ["Onkyo AV Receiver"]),
        ("TransportBasic", ["NVIDIA Shield"]),
    ]
    assert fake.sent == [] and fake.started == []  # read-only: nothing went to the hub


async def test_describe_activity_lists_devices_it_turns_off(mcp_client):
    d = (await mcp_client.call_tool("harmony_describe_activity", {"activity": "Listen to Music"})).structured_content
    assert {x["name"]: x["power"] for x in d["devices"]} == {
        "Onkyo AV Receiver": "on",
        "NVIDIA Shield": "off",
        "Living Room TV": "off",
    }


async def test_describe_activity_publishes_its_schema(mcp_client):
    t = (await tools(mcp_client))["harmony_describe_activity"]
    assert t.input_schema["required"] == ["activity"]
    device = t.output_schema["$defs"]["ExpectedDevice"]
    assert device["properties"]["power"]["enum"] == ["on", "off", "unknown"]


async def test_describe_an_unknown_activity_lists_known_ones(mcp_client):
    result = await mcp_client.call_tool("harmony_describe_activity", {"activity": "Watch Netflix"})
    assert result.is_error and "Unknown activity 'Watch Netflix'" in text(result)


# --- /healthz under serve --http -----------------------------------------------
async def healthz() -> httpx2.Response:
    app = remote.build_app(server.mcp, remote.HttpConfig(), health_check=cli.check_health)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
        return await http.get(remote.HEALTH_PATH)


async def test_healthz_is_ok_while_a_hub_is_connected(mcp_client):
    resp = await healthz()
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_healthz_is_degraded_when_every_hub_is_offline(mcp_client, fake):
    fake.push_disconnect()
    resp = await healthz()
    assert resp.status_code == 503 and resp.json()["status"] == "degraded"
    assert "Living Room" not in resp.text  # the endpoint is public: no hub names


async def test_healthz_is_degraded_before_the_server_has_started():
    assert server._hubs is None
    assert (await healthz()).status_code == 503


async def test_send_command_routes_through_the_activity(mcp_client, fake):
    # The model says "volume up"; the activity knows volume lives on the receiver.
    await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Shield"})
    result = await mcp_client.call_tool("harmony_send_command", {"command": "volume up", "repeat": 2})
    assert detail(result) == "Send VolumeUp x2 to Onkyo AV Receiver on Living Room"
    await mcp_client.call_tool("harmony_send_command", {"command": "Pause"})
    assert fake.presses() == [(ONKYO, "VolumeUp"), (ONKYO, "VolumeUp"), (SHIELD, "Pause")]


async def test_send_command_to_a_device_works_while_off(mcp_client, fake):
    result = await mcp_client.call_tool("harmony_send_command", {"command": "PowerOn", "device": "Onkyo AV Receiver"})
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
        {"command": "VolumeUp", "device": "Onkyo AV Receiver", "hub": "Garage"},
    ],
)
async def test_bad_sends_never_reach_the_hub(mcp_client, fake, args):
    result = await mcp_client.call_tool("harmony_send_command", args)
    assert result.is_error
    assert fake.sent == []


async def test_a_command_from_another_device_is_not_a_back_door(mcp_client, fake):
    # PowerToggle exists, but on the TV: naming the Shield must not find it.
    result = await mcp_client.call_tool("harmony_send_command", {"command": "PowerToggle", "device": "NVIDIA Shield"})
    assert result.is_error and "has no 'PowerToggle'" in text(result)
    assert fake.sent == []


async def test_unreachable_hub_explains_itself(mcp_client, fake):
    fake.push_disconnect()
    result = await mcp_client.call_tool("harmony_send_command", {"command": "PowerOn", "device": "Onkyo AV Receiver"})
    assert result.is_error and "Can't reach the Harmony hub 'Living Room'" in text(result)


async def test_unconfigured_server_still_answers_status(tmp_path, monkeypatch, factory):
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    c = await connect(monkeypatch, factory)
    try:
        status = (await c.call_tool("harmony_get_status", {})).structured_content
        assert status["hubs"] == [] and "check --host" in status["hint"]
        result = await c.call_tool("harmony_power_off", {})
        assert result.is_error and "check --host" in text(result)
    finally:
        await c.__aexit__(None, None, None)
