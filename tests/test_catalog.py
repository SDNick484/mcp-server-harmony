"""The hub config -> allow-list parsing. Pure functions, no fakes needed."""

from __future__ import annotations

import json

from harmony_mcp.catalog import Catalog

from .conftest import ONKYO, SAMPLE_CONFIG, SHIELD, TV, WATCH_SHIELD


def test_power_off_pseudo_activity_is_not_listed():
    names = [a.name for a in Catalog.from_config(SAMPLE_CONFIG).activities]
    assert names == ["Watch Shield", "Listen to Music"]


def test_string_ids_become_ints():
    # The config says "38000001"; aioharmony's API wants 38000001.
    cat = Catalog.from_config(SAMPLE_CONFIG)
    a = cat.activity("Watch Shield")
    assert a is not None and a.activity_id == WATCH_SHIELD
    assert all(isinstance(c.device_id, int) for c in a.commands)


def test_activity_commands_route_to_their_devices():
    a = Catalog.from_config(SAMPLE_CONFIG).activity("watch shield")
    assert a is not None
    routes = {c.name: c.device_id for c in a.commands}
    assert routes == {"VolumeUp": ONKYO, "Mute": ONKYO, "Pause": SHIELD, "Play": SHIELD}


def test_names_match_loosely():
    cat = Catalog.from_config(SAMPLE_CONFIG)
    d = cat.device("onkyo av receiver")
    assert d is not None
    for spelling in ["VolumeUp", "volume up", "Volume Up", "volume_up", "VOLUME-UP"]:
        cmd = Catalog.find_command(d.commands, spelling)
        assert cmd is not None and cmd.name == "VolumeUp", spelling


def test_unknown_names_resolve_to_none():
    cat = Catalog.from_config(SAMPLE_CONFIG)
    assert cat.activity("Watch Netflix") is None
    assert cat.device("Xbox") is None
    d = cat.device("NVIDIA Shield")
    assert d is not None and Catalog.find_command(d.commands, "SelfDestruct") is None


def test_unparseable_functions_are_skipped_not_fatal():
    config = {
        "activity": [],
        "device": [
            {
                "id": "1",
                "label": "Odd",
                "controlGroup": [
                    {
                        "name": "G",
                        "function": [
                            {"name": "Bad", "action": "not json"},
                            {"name": "NoDevice", "action": json.dumps({"command": "X"})},
                            {"name": "Good", "action": json.dumps({"command": "Good", "deviceId": "1"})},
                        ],
                    }
                ],
            }
        ],
    }
    d = Catalog.from_config(config).device("Odd")
    assert d is not None and [c.name for c in d.commands] == ["Good"]


def test_empty_config():
    assert Catalog.from_config(None) == Catalog()
    assert Catalog.from_config({}).activities == ()


# --- an activity's expected state (fixit, ASSUMPTION H-FIXIT) -------------------------------
def _fixit_config(fixit: object) -> dict:
    """One activity routing Volume to device 1, with the given fixit, on a hub with devices 1 and 2."""
    vol = {"name": "VolumeUp", "action": json.dumps({"command": "VolumeUp", "deviceId": "1"})}
    return {
        "activity": [
            {"id": "5", "label": "Watch", "controlGroup": [{"name": "Volume", "function": [vol]}], "fixit": fixit}
        ],
        "device": [{"id": "1", "label": "Receiver"}, {"id": "2", "label": "TV"}],
    }


def test_fixit_is_parsed_into_expected_states():
    a = Catalog.from_config(SAMPLE_CONFIG).activity("Watch Shield")
    assert a is not None and a.expected is not None
    by_id = {e.device_id: e for e in a.expected}
    assert (by_id[ONKYO].power, by_id[ONKYO].input) == ("on", "STRM BOX")
    assert (by_id[SHIELD].power, by_id[SHIELD].input) == ("on", None)
    assert by_id[TV].relative_power and not by_id[ONKYO].relative_power


def test_fixit_shapes_seen_in_real_dumps():
    # From published dumps: an empty Input, a hand-powered device with no Power at all, and the flags.
    cat = Catalog.from_config(
        _fixit_config(
            {
                "1": {"id": "1", "Power": "On", "Input": "", "isRelativePower": True, "isRelativeInput": True},
                "2": {"id": "2", "isManualPower": True},
            }
        )
    )
    a = cat.activity("Watch")
    assert a is not None
    one, two = cat.expected_states(a)
    assert (one.power, one.input, one.relative_power, one.relative_input) == ("on", None, True, True)
    assert (two.power, two.manual_power) == ("unknown", True)  # manual is not "on" or "off"


def test_without_fixit_the_expected_state_is_unknown():
    for fixit in (None, "garbage", [], {}, {"1": "not an object"}):
        cat = Catalog.from_config(_fixit_config(fixit))
        a = cat.activity("Watch")
        assert a is not None and a.expected is None, fixit
        # The routed device is still reported, as unknown: never assumed on.
        assert [(e.device_id, e.power, e.input) for e in cat.expected_states(a)] == [(1, "unknown", None)]


def test_a_bad_fixit_entry_is_skipped_and_the_rest_kept():
    cat = Catalog.from_config(
        _fixit_config(
            {"x": {"id": "not a number", "Power": "On"}, "2": {"id": "2", "Power": "Sideways"}, "3": ["nope"]}
        )
    )
    a = cat.activity("Watch")
    assert a is not None and a.expected is not None
    # Device 1 had no usable entry but its buttons route to it: unknown. Device 2's odd Power: unknown.
    assert [(e.device_id, e.power) for e in cat.expected_states(a)] == [(1, "unknown"), (2, "unknown")]


def test_routes_group_buttons_by_device():
    cat = Catalog.from_config(SAMPLE_CONFIG)
    a = cat.activity("Watch Shield")
    assert a is not None
    assert cat.routes(a) == (("Volume", (ONKYO,)), ("TransportBasic", (SHIELD,)))
