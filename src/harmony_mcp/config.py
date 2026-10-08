"""Settings: which hubs, and what to call them.

A Harmony hub's local API needs no pairing and no credentials: anything on the
LAN that can reach TCP 8088 (websocket API) can control it. So, unlike
mcp-server-shieldtv, there is nothing secret to store, only addresses.

One server can drive several hubs (say, one per TV). ``config.json``::

    {
      "hubs": [
        {"host": "192.168.1.60", "name": "Living Room"},
        {"host": "192.168.1.61"}
      ]
    }

``name`` is optional. Each hub has its own name, set in the Harmony app
(aioharmony reports it as ``friendlyName``), and ``check`` saves that name here
the first time it sees the hub, so the name is known even when a hub is
offline at startup. A name you write yourself is kept; ``check`` never
overwrites one.

``HARMONY_HOSTS`` (comma-separated) or ``HARMONY_HOST`` replaces the file's
list, picking up names from the file for hosts it also lists. The old
single-hub form ``{"host": "..."}`` still works.
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
class HubSettings:
    host: str
    name: str | None = None  # from config.json; None until `check` saves the hub's own name


@dataclass(frozen=True)
class Settings:
    hubs: tuple[HubSettings, ...]
    protocol: Protocol | None = None

    @property
    def configured(self) -> bool:
        return bool(self.hubs)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_hubs(data: dict[str, Any]) -> list[HubSettings]:
    """The hubs in config.json, in order, without duplicates."""
    entries: list[Any] = list(data.get("hubs") or [])
    if isinstance(data.get("host"), str):  # the single-hub form
        entries.insert(0, data["host"])
    hubs: list[HubSettings] = []
    for e in entries:
        if isinstance(e, str) and e.strip():
            hub = HubSettings(e.strip())
        elif isinstance(e, dict) and isinstance(e.get("host"), str) and e["host"].strip():
            name = e.get("name")
            hub = HubSettings(e["host"].strip(), name.strip() if isinstance(name, str) and name.strip() else None)
        else:
            log.warning("Ignoring hub entry %r in config.json: expected a host or {host, name}", e)
            continue
        if all(h.host != hub.host for h in hubs):
            hubs.append(hub)
    return hubs


def load_settings() -> Settings:
    data = _read_json(config_dir() / "config.json")
    hubs = _file_hubs(data)
    env = os.environ.get("HARMONY_HOSTS") or os.environ.get("HARMONY_HOST")
    if env:
        names = {h.host: h.name for h in hubs}
        hosts = list(dict.fromkeys(h.strip() for h in env.split(",") if h.strip()))
        hubs = [HubSettings(h, names.get(h)) for h in hosts]
    protocol = data.get("protocol")
    if protocol not in (None, "WEBSOCKETS", "XMPP"):
        log.warning("Ignoring protocol %r in config.json: expected WEBSOCKETS or XMPP", protocol)
        protocol = None
    return Settings(hubs=tuple(hubs), protocol=protocol)


def save_hub(host: str, name: str | None) -> Path:
    """Add a hub to config.json, or fill in its name if it has none yet.

    Keeps every other hub and key. Rewrites the single-hub form as a list.
    """
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.json"
    data = _read_json(path)
    hubs = _file_hubs(data)
    data.pop("host", None)
    for i, h in enumerate(hubs):
        if h.host == host:
            if h.name is None and name:
                hubs[i] = HubSettings(host, name)
            break
    else:
        hubs.append(HubSettings(host, name))
    data["hubs"] = [{"host": h.host, "name": h.name} if h.name else {"host": h.host} for h in hubs]
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
