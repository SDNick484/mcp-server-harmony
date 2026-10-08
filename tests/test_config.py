"""Where settings come from, and in what order."""

from __future__ import annotations

import json

from harmony_mcp.config import HubSettings, load_settings, save_hub

from .conftest import DEN, LIVING


def test_hub_list(two_hubs):
    assert load_settings().hubs == (HubSettings(LIVING), HubSettings(DEN, "Den"))


def test_single_hub_form_still_works(config_dir):
    (config_dir / "config.json").write_text(json.dumps({"host": LIVING, "protocol": "WEBSOCKETS"}))
    s = load_settings()
    assert (s.hubs, s.protocol) == ((HubSettings(LIVING),), "WEBSOCKETS")


def test_environment_replaces_the_list_but_keeps_names(two_hubs, monkeypatch):
    monkeypatch.setenv("HARMONY_HOSTS", f"{DEN}, 192.0.2.99, {DEN}")
    assert load_settings().hubs == (HubSettings(DEN, "Den"), HubSettings("192.0.2.99"))
    monkeypatch.delenv("HARMONY_HOSTS")
    monkeypatch.setenv("HARMONY_HOST", LIVING)  # the old single-hub variable
    assert load_settings().hubs == (HubSettings(LIVING),)


def test_bad_entries_are_skipped(config_dir):
    (config_dir / "config.json").write_text(
        json.dumps({"hubs": [LIVING, {"name": "no host"}, 42, {"host": DEN, "name": "  "}], "protocol": "TELNET"})
    )
    s = load_settings()
    assert (s.hubs, s.protocol) == ((HubSettings(LIVING), HubSettings(DEN)), None)


def test_save_hub_adds_and_fills_names_but_never_renames(config_dir):
    (config_dir / "config.json").write_text(json.dumps({"host": LIVING, "protocol": "XMPP"}))
    save_hub(DEN, "Den")
    save_hub(LIVING, "Living Room")  # fills the missing name
    save_hub(DEN, "Renamed In The App")  # a name you have is kept
    data = json.loads((config_dir / "config.json").read_text())
    assert data == {
        "protocol": "XMPP",
        "hubs": [{"host": LIVING, "name": "Living Room"}, {"host": DEN, "name": "Den"}],
    }


def test_missing_or_corrupt_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    assert load_settings().configured is False
    (tmp_path / "config.json").write_text("{not json")
    assert load_settings().hubs == ()
