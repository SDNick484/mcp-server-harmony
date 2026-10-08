"""Where settings come from, and in what order."""

from __future__ import annotations

import json

from harmony_mcp.config import load_settings, save_host


def test_most_specific_host_wins(config_dir, monkeypatch):
    assert load_settings().host == "192.0.2.20"  # config.json
    monkeypatch.setenv("HARMONY_HOST", "192.0.2.21")
    assert load_settings().host == "192.0.2.21"  # environment beats the file
    assert load_settings("192.0.2.22").host == "192.0.2.22"  # `check --host` beats both


def test_bad_protocol_is_ignored(config_dir):
    (config_dir / "config.json").write_text(json.dumps({"host": "h", "protocol": "TELNET"}))
    assert load_settings().protocol is None


def test_save_host_keeps_other_keys(config_dir):
    (config_dir / "config.json").write_text(json.dumps({"host": "old", "protocol": "WEBSOCKETS"}))
    save_host("192.0.2.30")
    assert json.loads((config_dir / "config.json").read_text()) == {"host": "192.0.2.30", "protocol": "WEBSOCKETS"}


def test_missing_or_corrupt_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    assert load_settings().configured is False
    (tmp_path / "config.json").write_text("{not json")
    assert load_settings().host is None
