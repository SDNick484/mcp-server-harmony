"""Two hubs, two TVs: which hub a call is about.

The rule under test: with no hub given, act when exactly one hub matches;
when several do, refuse and name them. Guessing would mean the wrong room's TV.
Both hubs have a "Listen to Music" activity, on purpose.
"""

from __future__ import annotations

import pytest

from .conftest import DEN, DEN_MUSIC, DEN_TV, DEN_WATCH, LIVING, ONKYO, WATCH_SHIELD
from .test_tools import connect, detail, outcomes, text

pytestmark = pytest.mark.anyio


@pytest.fixture
async def mcp_client(two_hubs, factory, monkeypatch):
    c = await connect(monkeypatch, factory)
    yield c
    await c.__aexit__(None, None, None)


async def test_status_lists_both_hubs_by_name(mcp_client):
    hubs = (await mcp_client.call_tool("get_status", {})).structured_content["hubs"]
    # Living Room's name is the hub's own; the Den's comes from config.json.
    assert [(h["hub"], h["host"]) for h in hubs] == [("Living Room", LIVING), ("Den", DEN)]


async def test_unique_names_need_no_hub(mcp_client, fakes):
    assert detail(await mcp_client.call_tool("start_activity", {"activity": "Watch TV"})) == "Start Watch TV on Den"
    assert fakes[DEN].started == [DEN_WATCH] and fakes[LIVING].started == []


async def test_a_repeated_name_asks_which_hub(mcp_client, fakes):
    result = await mcp_client.call_tool("start_activity", {"activity": "Listen to Music"})
    assert result.is_error
    assert "exists on Living Room, Den; pass hub to choose" in text(result)
    assert fakes[DEN].started == [] and fakes[LIVING].started == []
    result = await mcp_client.call_tool("start_activity", {"activity": "Listen to Music", "hub": "den"})
    assert detail(result) == "Start Listen to Music on Den"
    assert fakes[DEN].started == [DEN_MUSIC]


async def test_hub_by_address_works_too(mcp_client, fakes):
    await mcp_client.call_tool("start_activity", {"activity": "Listen to Music", "hub": LIVING})
    assert fakes[LIVING].started != []


async def test_buttons_follow_the_one_running_activity(mcp_client, fakes):
    await mcp_client.call_tool("start_activity", {"activity": "Watch TV"})
    result = await mcp_client.call_tool("send_command", {"command": "VolumeUp"})
    assert detail(result) == "Send VolumeUp x1 to Den TV on Den"
    assert fakes[DEN].presses() == [(DEN_TV, "VolumeUp")] and fakes[LIVING].sent == []


async def test_two_running_activities_need_a_hub(mcp_client, fakes):
    await mcp_client.call_tool("start_activity", {"activity": "Watch TV"})
    await mcp_client.call_tool("start_activity", {"activity": "Watch Shield"})
    result = await mcp_client.call_tool("send_command", {"command": "VolumeUp"})
    assert result.is_error
    assert "running on Living Room (Watch Shield), Den (Watch TV); pass hub" in text(result)
    result = await mcp_client.call_tool("send_command", {"command": "VolumeUp", "hub": "Living Room"})
    assert detail(result) == "Send VolumeUp x1 to Onkyo AV Receiver on Living Room"
    assert fakes[LIVING].presses() == [(ONKYO, "VolumeUp")] and fakes[DEN].sent == []


async def test_list_commands_for_a_repeated_name(mcp_client):
    result = await mcp_client.call_tool("list_commands", {"target": "Listen to Music"})
    assert result.is_error and "matches Living Room (activity Listen to Music), Den (activity" in text(result)
    listing = (
        await mcp_client.call_tool("list_commands", {"target": "Listen to Music", "hub": "Den"})
    ).structured_content
    assert (listing["hub"], [c["device"] for c in listing["commands"]]) == ("Den", ["Den TV"])


async def test_power_off_with_one_hub_on(mcp_client, fakes):
    await mcp_client.call_tool("start_activity", {"activity": "Watch TV"})
    assert outcomes(await mcp_client.call_tool("power_off", {})) == [("Den", "done")]
    assert fakes[LIVING].started == []


async def test_power_off_with_both_on_asks_or_takes_all(mcp_client, fakes):
    await mcp_client.call_tool("start_activity", {"activity": "Watch TV"})
    await mcp_client.call_tool("start_activity", {"activity": "Watch Shield"})
    result = await mcp_client.call_tool("power_off", {})
    assert result.is_error and "Several hubs are on (Living Room, Den); pass hub, or hub='all'" in text(result)
    assert outcomes(await mcp_client.call_tool("power_off", {"hub": "all"})) == [
        ("Living Room", "done"),
        ("Den", "done"),
    ]
    assert fakes[LIVING].started == [WATCH_SHIELD, -1] and fakes[DEN].started == [DEN_WATCH, -1]


async def test_power_off_all_reports_a_hub_it_cannot_reach(mcp_client, fakes):
    await mcp_client.call_tool("start_activity", {"activity": "Watch Shield"})
    fakes[DEN].push_disconnect()
    result = await mcp_client.call_tool("power_off", {"hub": "ALL"})
    assert outcomes(result) == [("Living Room", "done"), ("Den", "error")]  # the Living Room did turn off
    assert "Can't reach the Harmony hub 'Den'" in result.structured_content["results"][1]["detail"]


async def test_list_tools_can_filter_by_hub(mcp_client):
    rows = (await mcp_client.call_tool("list_activities", {})).structured_content["result"]
    assert {(r["hub"], r["name"]) for r in rows} == {
        ("Living Room", "Watch Shield"),
        ("Living Room", "Listen to Music"),
        ("Den", "Watch TV"),
        ("Den", "Listen to Music"),
    }
    rows = (await mcp_client.call_tool("list_devices", {"hub": "Den"})).structured_content["result"]
    assert [r["name"] for r in rows] == ["Den TV"]


async def test_an_offline_hub_is_named_in_unknown_name_errors(mcp_client, fakes):
    fakes[DEN].push_disconnect()
    # The Den connected once, so its catalog is cached; force the "never connected" case.
    from harmony_mcp import server
    from harmony_mcp.catalog import Catalog

    server.hubs().hubs[1].catalog = Catalog()
    result = await mcp_client.call_tool("start_activity", {"activity": "Watch TV"})
    assert result.is_error and "(Den not connected, so its names are unknown.)" in text(result)
