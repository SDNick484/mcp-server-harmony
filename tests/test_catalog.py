"""The hub config -> allow-list parsing. Pure functions, no fakes needed."""

from __future__ import annotations

import json

from harmony_mcp.catalog import Catalog

from .conftest import ONKYO, SAMPLE_CONFIG, SHIELD, WATCH_SHIELD


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
