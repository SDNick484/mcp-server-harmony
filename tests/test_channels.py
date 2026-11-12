"""Tuning: harmony_tune_channel, channel favorites, and the exact-name lookup that makes '-' and '.' findable.

Fixtures: the Den's "Watch TV" activity routes 0-9, '-' and OK (label Select) to the Den TV, which has them
too; there is no '.'. The Living Room's "Watch Shield" routes no digits; its TV (a device) has 0-9, '-' and
'.', and no select button.
"""

from __future__ import annotations

import copy
import json

import pytest
from aioharmony.const import SendCommandDevice

from harmony_mcp import server
from harmony_mcp.catalog import Catalog
from harmony_mcp.config import HubSettings, load_settings, save_hub
from harmony_mcp.ratelimit import TokenBucket

from .conftest import DEN, DEN_CONFIG, DEN_TV, LIVING, SAMPLE_CONFIG, FakeHarmonyAPI
from .test_tools import connect, detail, text

pytestmark = pytest.mark.anyio

LIVING_TV = 71000003


@pytest.fixture
def favorites(two_hubs):
    """Both hubs, with a few favorites ("9-1" on the Den, "5.1" on the Living Room)."""
    (two_hubs / "config.json").write_text(
        json.dumps(
            {
                "hubs": [
                    {"host": LIVING, "channels": {"PBS": "5.1", "News": "4"}},
                    {"host": DEN, "name": "Den", "channels": {"ESPN": "702", "K Q E D": "9-1", "News": "11"}},
                ]
            }
        )
    )
    return two_hubs


@pytest.fixture
async def mcp_client(favorites, factory, monkeypatch):
    c = await connect(monkeypatch, factory)
    yield c
    await c.__aexit__(None, None, None)


async def tune(c, channel, **args):
    return await c.call_tool("harmony_tune_channel", {"channel": channel, **args})


async def watch_den_tv(c):
    await c.call_tool("harmony_start_activity", {"activity": "Watch TV"})


def pressed(fake: FakeHarmonyAPI) -> list[str]:
    return [name for _, name in fake.presses()]


# --- the number ---------------------------------------------------------------------------
async def test_a_plain_number_is_pressed_digit_by_digit_with_gaps(mcp_client, fakes):
    await watch_den_tv(mcp_client)
    result = await tune(mcp_client, "702")
    assert detail(result) == "Tune Den to channel 702 by pressing 7, 0, 2"
    assert result.structured_content["sent"] == ["holdAction press+release Den TV/" + d for d in "702"]
    sent = fakes[DEN].sent
    assert [type(x) is SendCommandDevice for x in sent] == [True, False, True, False, True]  # press, pause, ...
    assert all(isinstance(x, float) for x in sent[1::2])
    assert {p[0] for p in fakes[DEN].presses()} == {DEN_TV}  # routed by the activity to the TV


@pytest.mark.parametrize("sep", ["-", "."])
async def test_a_sub_channel_presses_the_separator_it_was_given(mcp_client, fakes, sep):
    result = await tune(mcp_client, f"5{sep}1", device="Living Room TV")
    assert detail(result).endswith(f"pressing 5, {sep}, 1")
    assert pressed(fakes[LIVING]) == ["5", sep, "1"]
    assert {p[0] for p in fakes[LIVING].presses()} == {LIVING_TV}


async def test_a_missing_separator_falls_back_to_the_other_one(mcp_client, fakes):
    # The Den TV has '-' but no '.', like the Bedroom TV on the owner's hubs.
    await watch_den_tv(mcp_client)
    await tune(mcp_client, "5.1")
    assert pressed(fakes[DEN]) == ["5", "-", "1"]


async def test_no_separator_at_all_refuses_before_sending(factory, fakes, favorites, monkeypatch):
    cfg = copy.deepcopy(DEN_CONFIG)
    for dev in cfg["device"]:
        for g in dev["controlGroup"]:
            g["function"] = [f for f in g["function"] if f["name"] != "-"]
    fakes[DEN].config = cfg
    c = await connect(monkeypatch, factory)
    try:
        result = await tune(c, "5-1", device="Den TV")
        assert result.is_error and "no separator ('-' or '.') button, which channel 5-1 needs" in text(result)
        assert fakes[DEN].sent == []
        assert detail(await tune(c, "51", device="Den TV")) == "Tune Den to channel 51 by pressing 5, 1"
    finally:
        await c.__aexit__(None, None, None)


@pytest.mark.parametrize("bad", ["5.1.2", "-5", "5.", "1234567", "12-3456789"])
async def test_a_malformed_number_is_refused_as_a_number(mcp_client, fakes, bad):
    await watch_den_tv(mcp_client)
    result = await tune(mcp_client, bad)
    assert result.is_error and ("channel number" in text(result) or "digits" in text(result))
    assert fakes[DEN].sent == []


# --- favorites ----------------------------------------------------------------------------
async def test_a_favorite_name_finds_its_hub_and_number(mcp_client, fakes):
    await watch_den_tv(mcp_client)
    result = await tune(mcp_client, "espn")  # loose match, like every other name
    assert detail(result) == "Tune Den to channel 702 (ESPN) by pressing 7, 0, 2"
    assert pressed(fakes[DEN]) == ["7", "0", "2"]
    # A favorite stored with a space in its name, and a '-' number.
    await tune(mcp_client, "kqed")
    assert pressed(fakes[DEN])[3:] == ["9", "-", "1"]


async def test_an_unknown_name_lists_the_configured_ones(mcp_client, fakes):
    result = await tune(mcp_client, "ESPM")
    assert result.is_error
    msg = text(result)
    assert "Unknown channel 'ESPM'" in msg and "Living Room: PBS, News" in msg and "Den: ESPN, K Q E D, News" in msg
    assert fakes[DEN].sent == [] and fakes[LIVING].sent == []


async def test_a_name_on_two_hubs_needs_a_hub_unless_one_is_on(mcp_client, fakes):
    result = await tune(mcp_client, "News")
    assert result.is_error and "favorite on Living Room, Den; pass hub to choose" in text(result)
    # With one hub's activity running, that hub is the one the call is about.
    await watch_den_tv(mcp_client)
    assert detail(await tune(mcp_client, "News")) == "Tune Den to channel 11 (News) by pressing 1, 1"
    assert detail(await tune(mcp_client, "News", hub="Living Room", device="Living Room TV")).startswith(
        "Tune Living Room to channel 4 (News)"
    )


async def test_no_favorites_says_how_to_add_them(settings, factory, monkeypatch):
    c = await connect(monkeypatch, factory)
    try:
        result = await tune(c, "ESPN")
        assert result.is_error and 'Add favorites under "channels"' in text(result)
    finally:
        await c.__aexit__(None, None, None)


# --- where the digits come from -----------------------------------------------------------
async def test_an_activity_without_digits_says_so_and_what_to_do(mcp_client, fakes):
    await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Shield"})
    result = await tune(mcp_client, "702")
    msg = text(result)
    assert result.is_error
    assert "activity Watch Shield on Living Room has no 7, 0, 2 button" in msg
    assert "Pass device= one of: Living Room TV" in msg and "start an activity that routes the digit buttons" in msg
    assert fakes[LIVING].sent == []


async def test_a_device_overrides_the_activity(mcp_client, fakes):
    await mcp_client.call_tool("harmony_start_activity", {"activity": "Watch Shield"})
    result = await tune(mcp_client, "9", device="Living Room TV")
    assert detail(result) == "Tune Living Room to channel 9 by pressing 9"
    assert fakes[LIVING].presses() == [(LIVING_TV, "9")]


async def test_a_device_without_digits_is_refused(mcp_client, fakes):
    result = await tune(mcp_client, "9", device="Onkyo AV Receiver")
    assert result.is_error and "device Onkyo AV Receiver on Living Room has no 9 button" in text(result)
    assert "Pass device= one of: Living Room TV" in text(result)
    assert fakes[LIVING].sent == []


async def test_no_running_activity_and_no_device(mcp_client):
    result = await tune(mcp_client, "702", hub="Den")
    assert result.is_error and "No activity is running on Den" in text(result)


# --- enter --------------------------------------------------------------------------------
async def test_enter_presses_select_on_the_same_device_after_the_digits(mcp_client, fakes):
    await watch_den_tv(mcp_client)
    result = await tune(mcp_client, "42", enter=True)
    assert detail(result) == "Tune Den to channel 42 by pressing 4, 2, OK"  # OK, labelled Select
    assert fakes[DEN].presses()[-1] == (DEN_TV, "OK")
    assert len(fakes[DEN].presses()) == 3


async def test_enter_without_a_select_button_says_so_and_sends_nothing(mcp_client, fakes):
    result = await tune(mcp_client, "42", device="Living Room TV", enter=True)
    assert result.is_error and "no select button (OK, Select, Enter) for Living Room TV" in text(result)
    assert fakes[LIVING].sent == []  # not even the digits: nothing is half-sent
    # Without enter the same call works.
    assert not (await tune(mcp_client, "42", device="Living Room TV")).is_error


# --- the guards ---------------------------------------------------------------------------
async def test_refused_while_the_hub_is_busy(mcp_client, fakes):
    await watch_den_tv(mcp_client)
    fakes[DEN].push_starting(39000002)
    result = await tune(mcp_client, "702")
    assert result.is_error and "Den is busy starting Listen to Music; tune 702 once it finishes" in text(result)
    assert fakes[DEN].sent == []


async def test_the_rate_limit_refuses_the_whole_number(mcp_client, fakes):
    await watch_den_tv(mcp_client)
    den = next(h for h in server.hubs().hubs if h.name == "Den")
    now = [0.0]
    den.press_bucket = TokenBucket(capacity=5, rate=1.0, clock=lambda: now[0])
    assert not (await tune(mcp_client, "70")).is_error  # 2 of 5 tokens, one charge for the whole number
    result = await tune(mcp_client, "5-1", enter=True)  # 3 digits and separator + OK = 4 > 3 left
    assert result.is_error and "Rate limit on Den: refusing to tune 5-1" in text(result)
    assert pressed(fakes[DEN]) == ["7", "0"]  # none of the refused call's presses went out
    now[0] = 10.0
    assert not (await tune(mcp_client, "5-1", enter=True)).is_error


async def test_a_rejected_press_is_reported_with_what_already_went_out(mcp_client, fakes):
    await watch_den_tv(mcp_client)
    fakes[DEN].failing_commands = {"0"}
    result = await tune(mcp_client, "702")
    assert result.is_error and "rejected 0 for Den TV while tune 702" in text(result)
    assert "presses before it were sent" in text(result)


async def test_dry_run_reports_one_sequence_and_sends_nothing(favorites, factory, monkeypatch, fakes):
    monkeypatch.setenv("HARMONY_DRY_RUN", "1")
    c = await connect(monkeypatch, factory)
    try:
        den = next(h for h in server.hubs().hubs if h.name == "Den")
        den.activity_id = 39000001  # a dry run starts nothing, so say the activity is on
        result = await tune(c, "5-1", enter=True)
        sc = result.structured_content
        assert sc["outcome"] == "dry_run" and sc["detail"].startswith("DRY RUN, nothing sent: would tune Den")
        assert sc["sent"] == ["holdAction press+release Den TV/" + b for b in ["5", "-", "1", "OK"]]
        assert fakes[DEN].sent == []
    finally:
        await c.__aexit__(None, None, None)


# --- the lookup fix -----------------------------------------------------------------------
def test_dash_and_dot_are_found_by_their_exact_names():
    tv = next(d for d in Catalog.from_config(SAMPLE_CONFIG).devices if d.name == "Living Room TV")
    assert Catalog.find_command(tv.commands, "-").name == "-"
    assert Catalog.find_command(tv.commands, ".").name == "."
    assert Catalog.find_command(tv.commands, "5").name == "5"


def test_a_punctuation_name_never_matches_by_normalising():
    # Only '-' exists: '.' must not be answered with it just because both normalise to "".
    tv = next(d for d in Catalog.from_config(DEN_CONFIG).devices if d.name == "Den TV")
    assert Catalog.find_command(tv.commands, "-").name == "-"
    assert Catalog.find_command(tv.commands, ".") is None
    assert Catalog.find_command(tv.commands, "volume up").name == "VolumeUp"  # the loose match still works


async def test_send_command_presses_the_right_one_of_dash_and_dot(mcp_client, fakes):
    await mcp_client.call_tool("harmony_send_command", {"command": ".", "device": "Living Room TV"})
    await mcp_client.call_tool("harmony_send_command", {"command": "-", "device": "Living Room TV"})
    assert pressed(fakes[LIVING]) == [".", "-"]


# --- config -------------------------------------------------------------------------------
def test_channels_load_per_hub(favorites):
    s = load_settings()
    assert s.hubs[0].channels == {"PBS": "5.1", "News": "4"}
    assert s.hubs[1].channels == {"ESPN": "702", "K Q E D": "9-1", "News": "11"}
    assert s.problems == ()


def test_bad_channels_are_reported_and_skipped(config_dir):
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "hubs": [
                    {
                        "host": LIVING,
                        "channels": {
                            "ESPN": "702",
                            "espn": "703",  # the same name once matched loosely
                            "Five": 5,  # a JSON number
                            "Long": "1234567",
                            "Odd": "5.1.2",
                            "Digits": "7",
                            "702": "702",
                            "!!!": "7",
                        },
                    },
                    {"host": DEN, "channels": ["ESPN"]},
                ]
            }
        )
    )
    s = load_settings()
    assert s.hubs == (HubSettings(LIVING, None, {"ESPN": "702", "Digits": "7"}), HubSettings(DEN))
    joined = "\n".join(s.problems)
    for needle in ("'espn'", "'Five'", "'Long'", "more than 6 digits", "'Odd'", "'702'", "'!!!'", "expected an object"):
        assert needle in joined, needle


def test_the_environment_keeps_each_hubs_channels(favorites, monkeypatch):
    monkeypatch.setenv("HARMONY_HOSTS", DEN)
    assert load_settings().hubs[0].channels["ESPN"] == "702"


def test_check_keeps_channels_as_written_even_the_bad_ones(config_dir):
    raw = {"ESPN": "702", "Broken": 7}
    (config_dir / "config.json").write_text(json.dumps({"hubs": [{"host": LIVING, "channels": raw}]}))
    save_hub(LIVING, "Living Room")
    save_hub(DEN, "Den")
    hubs = json.loads((config_dir / "config.json").read_text())["hubs"]
    assert hubs == [{"host": LIVING, "name": "Living Room", "channels": raw}, {"host": DEN, "name": "Den"}]


async def test_the_hub_resource_shows_the_favorites(mcp_client):
    doc = json.loads((await mcp_client.read_resource("harmony://hubs/Den")).contents[0].text)
    assert doc["channels"] == {"ESPN": "702", "K Q E D": "9-1", "News": "11"}
    everything = json.loads((await mcp_client.read_resource("harmony://hubs")).contents[0].text)
    assert [h["channels"] for h in everything][0] == {"PBS": "5.1", "News": "4"}
