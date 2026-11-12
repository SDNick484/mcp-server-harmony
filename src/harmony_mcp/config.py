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

A hub may also list channel favorites, a name for each number harmony_tune_channel can press::

    {"host": "192.168.1.60", "name": "Living Room", "channels": {"ESPN": "702", "KQED": "9.1"}}

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .channels import favorite, looks_like_number, number_problem

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
    # Favorite channels, name -> number ("ESPN": "702"). Channel lineups differ per TV provider, so
    # they belong to a hub, not to the server. Validated in _channels; only good entries get here.
    channels: dict[str, str] = field(default_factory=dict)


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
    # Record activity changes (history.py), in memory and in history.jsonl in the config directory.
    # HARMONY_HISTORY=off or "history": false in config.json turns it off.
    history: bool = True
    # Problems found while loading, as sentences; logged at startup and shown by `doctor`.
    problems: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.hubs)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


_OFF = ("0", "false", "no", "off")
_ON = ("1", "true", "yes", "on")


def _history_enabled(env: str | None, data: dict[str, Any], problems: list[str]) -> bool:
    """HARMONY_HISTORY wins over config.json's "history"; the default is on."""
    if env is not None and env.strip():
        word = env.strip().lower()
        if word in _OFF:
            return False
        if word in _ON:
            return True
        problems.append(f"ignoring HARMONY_HISTORY={env!r}: expected on or off")
    value = data.get("history")
    if value is None or isinstance(value, bool):
        return value is not False
    problems.append(f"ignoring history {value!r} in config.json: expected true or false")
    return True


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


def _channels(raw: Any, hub: str, problems: list[str]) -> dict[str, str]:
    """A hub's "channels" map, keeping the entries that are a name and a channel number."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(f'ignoring "channels" for hub {hub} in config.json: expected an object like {{"ESPN": "702"}}')
        return {}
    out: dict[str, str] = {}
    for name, number in raw.items():
        # A JSON number would lose "5.10" to 5.1, and a float can't say "5-1"; the value is typed as text.
        if not isinstance(number, str):
            problems.append(f'ignoring channel {name!r} for hub {hub}: the number must be a string like "702" or "5.1"')
        elif looks_like_number(name):
            # harmony_tune_channel reads digits-and-separators as a number before it looks at favorites.
            problems.append(f"ignoring channel {name!r} for hub {hub}: a name made of digits would read as a number")
        elif not any(ch.isalnum() for ch in name):
            problems.append(f"ignoring channel {name!r} for hub {hub}: the name needs a letter or a digit")
        elif (why := number_problem(number)) is not None:
            problems.append(f"ignoring channel {name!r} for hub {hub}: {why}")
        elif (dup := favorite(out, name)) is not None:
            problems.append(f"channels {dup[0]!r} and {name!r} for hub {hub} are the same name; using the first")
        else:
            out[name.strip()] = number.strip()
    return out


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
            host = e["host"].strip()
            hub = HubSettings(
                host,
                name.strip() if isinstance(name, str) and name.strip() else None,
                _channels(e.get("channels"), host, problems),
            )
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
        # The environment replaces the list of hubs, not what the file says about each one.
        known = {h.host: h for h in hubs}
        hubs = []
        for host in dict.fromkeys(h.strip() for h in env.split(",") if h.strip()):
            if (why := _host_problem(host)) is not None:
                problems.append(f"ignoring a hub from HARMONY_HOSTS: {why}")
            else:
                hubs.append(
                    HubSettings(host, known[host].name, known[host].channels) if host in known else HubSettings(host)
                )
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
        history=_history_enabled(os.environ.get("HARMONY_HISTORY"), data, problems),
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
    # Keep each hub's "channels" as written. Rewriting it from the parsed settings would silently drop
    # an entry that failed validation, and the owner's typo is theirs to fix, not ours to delete.
    raw_channels = {
        e["host"].strip(): e["channels"]
        for e in reversed(data.get("hubs") or [])
        if isinstance(e, dict) and isinstance(e.get("host"), str) and "channels" in e
    }
    data.pop("host", None)
    for i, h in enumerate(hubs):
        if h.host == host:
            if h.name is None and name:
                hubs[i] = HubSettings(host, name)
            break
    else:
        hubs.append(HubSettings(host, name))
    data["hubs"] = [
        {
            "host": h.host,
            **({"name": h.name} if h.name else {}),
            **({"channels": raw_channels[h.host]} if h.host in raw_channels else {}),
        }
        for h in hubs
    ]
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
