"""Settings and where they come from.

A Harmony hub's local API needs no pairing and no credentials: anything on the
LAN that can reach TCP 8088 (websocket API) can control it. So, unlike
mcp-server-shieldtv, there is nothing secret to store. The only setting is the
hub's address, from (most specific first) ``check --host``, ``HARMONY_HOST``,
or ``config.json``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

# The transports aioharmony speaks. None lets it try websockets first and fall
# back to XMPP. Late-2018 firmware turned local XMPP off by default (the app
# can re-enable it), so on a current hub websockets is what works; forcing it
# skips the fallback race.
Protocol = Literal["WEBSOCKETS", "XMPP"]


def config_dir() -> Path:
    override = os.environ.get("HARMONY_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mcp-server-harmony"


@dataclass(frozen=True)
class Settings:
    host: str | None
    protocol: Protocol | None = None

    @property
    def configured(self) -> bool:
        return bool(self.host)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_settings(host_override: str | None = None) -> Settings:
    data = _read_json(config_dir() / "config.json")
    host = host_override or os.environ.get("HARMONY_HOST") or data.get("host")
    protocol = data.get("protocol")
    if protocol not in (None, "WEBSOCKETS", "XMPP"):
        log.warning("Ignoring protocol %r in config.json: expected WEBSOCKETS or XMPP", protocol)
        protocol = None
    return Settings(host=host or None, protocol=protocol)


def save_host(host: str) -> Path:
    """Merge the host into config.json, keeping whatever else is there."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.json"
    data = _read_json(path)
    data["host"] = host
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
