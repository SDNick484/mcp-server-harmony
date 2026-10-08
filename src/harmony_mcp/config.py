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

Loading never fails: anything wrong becomes a sentence in
``Settings.problems`` (logged at startup, shown by `doctor`) and the bad part
is skipped, so one typo doesn't take every hub down.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
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
    # Overrides aioharmony's fixed port 8088 for *every* hub. Only useful
    # against the simulator (a real hub always listens on 8088).
    port: int | None = None
    # Read the hubs for real, but send nothing that changes anything; every
    # write reports what it would have sent (HARMONY_DRY_RUN=1, serve --dry-run).
    dry_run: bool = False
    # Problems found while loading, as sentences; logged at startup and shown by `doctor`.
    problems: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.hubs)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _host_problem(host: str) -> str | None:
    """Why `host` can't be a hub address, or None if it can."""
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if ":" in host:
        return (
            f"hub host {host!r} includes a port; aioharmony always uses 8088. Drop the port "
            '(a top-level "port" exists only for the simulator).'
        )
    if not _HOSTNAME.fullmatch(host):
        return f"hub host {host!r} is not an IP address or hostname"
    return None


_HOSTNAME = re.compile(
    r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?"
)


def _read_json(path: Path, problems: list[str] | None = None) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        if problems is not None:
            problems.append(f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg}); ignoring it")
        return {}
    if not isinstance(data, dict):
        if problems is not None:
            problems.append(f"{path} should hold a JSON object, not {type(data).__name__}; ignoring it")
        return {}
    return data


def _file_hubs(data: dict[str, Any], problems: list[str] | None = None) -> list[HubSettings]:
    """The hubs in config.json, in order, without duplicates."""
    problems = [] if problems is None else problems
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
            problems.append(f"ignoring hub entry {e!r} in config.json: expected a host or {{host, name}}")
            continue
        if (why := _host_problem(hub.host)) is not None:
            problems.append(f"ignoring a hub: {why}")
            continue
        if any(h.host == hub.host for h in hubs):
            problems.append(f"hub {hub.host} is listed twice in config.json; using the first entry")
            continue
        hubs.append(hub)
    return hubs


def load_settings() -> Settings:
    problems: list[str] = []
    data = _read_json(config_dir() / "config.json", problems)
    hubs = _file_hubs(data, problems)
    env = os.environ.get("HARMONY_HOSTS") or os.environ.get("HARMONY_HOST")
    if env:
        names = {h.host: h.name for h in hubs}
        hubs = []
        for host in dict.fromkeys(h.strip() for h in env.split(",") if h.strip()):
            if (why := _host_problem(host)) is not None:
                problems.append(f"ignoring a hub from HARMONY_HOSTS: {why}")
            else:
                hubs.append(HubSettings(host, names.get(host)))
    seen: dict[str, str] = {}
    for h in hubs:
        if h.name:
            key = "".join(ch for ch in h.name.lower() if ch.isalnum())
            if key in seen:
                problems.append(
                    f"two hubs are named {h.name!r} ({seen[key]} and {h.host}); give one a different name in "
                    "config.json, or tools can only tell them apart by address"
                )
            seen[key] = h.host
    protocol = data.get("protocol")
    if protocol not in (None, "WEBSOCKETS", "XMPP"):
        problems.append(f"ignoring protocol {protocol!r} in config.json: expected WEBSOCKETS or XMPP")
        protocol = None
    port = data.get("port")
    if port is not None and not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        problems.append(f"ignoring port {port!r} in config.json: expected a number from 1 to 65535")
        port = None
    return Settings(
        hubs=tuple(hubs),
        protocol=protocol,
        port=port,
        dry_run=_flag(os.environ.get("HARMONY_DRY_RUN")),
        problems=tuple(problems),
    )


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
