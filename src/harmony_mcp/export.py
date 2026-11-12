"""What an activity does, and `mcp-server-harmony export`: a hub's setup written down.

Logitech stopped making Harmony in 2021, and a hub can only be set up through
Logitech's cloud. Which devices you have, what each activity turns on and which
input it picks, which device each button goes to: all of that lives only on the
hub and in that cloud. This module writes it down, in two forms:

- ``describe_activity``: one activity as data. The ``harmony_describe_activity``
  tool returns it, and the worksheet below is built from it, so the model and the
  file can't disagree.
- ``export``: for each hub, ``<hub>.json`` (the raw config, account identifiers
  removed) and ``<hub>.md`` (a worksheet to rebuild the setup on another remote).

What it is *not*, said in the worksheet too: the hub's local API gives command
*names*, never IR codes, and nothing can be written back to a hub. It's a record
to rebuild from, not a backup you can restore.

The expected power and input come from each activity's ``fixit`` (ASSUMPTION
H-FIXIT). Where that's missing, every report says "unknown" rather than guessing.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from typing_extensions import TypedDict

from . import __version__
from .catalog import Activity, Catalog, Device, ExpectedState, Power
from .config import Settings
from .doctor import scrub
from .logsafe import redact

# Shown wherever an export or a description could be mistaken for a backup.
NOT_A_BACKUP = (
    "This is a record to rebuild from, not a backup: the hub's local API returns command names, not the IR "
    "codes behind them, and nothing can be written back to a hub. A new remote finds each device in its own "
    "database (by manufacturer and model) or learns the codes from the original remote."
)
UNKNOWN_NOTE = (
    "The hub's config has no readable expected state (fixit) for this activity, so power and input are "
    "unknown. Check them in the Harmony app or by watching the activity start; don't assume."
)


# --- describe one activity (the tool's output schema) ------------------------------------
class ExpectedDevice(TypedDict):
    name: str
    manufacturer: str
    model: str
    power: Power  # on | off | unknown, as the hub's fixit says; never inferred
    input: str | None  # e.g. "HDMI 1"; null when the hub names none or it's unknown
    notes: list[str]  # e.g. "power is a toggle: the hub tracks it, the device may disagree"


class ButtonRoute(TypedDict):
    group: str  # the hub's control group, e.g. "Volume", "TransportBasic", "NumericBasic"
    devices: list[str]  # the device(s) those buttons go to, normally one
    commands: list[str]  # the command names in that group


class ActivityDescription(TypedDict):
    hub: str
    name: str
    ref: str
    expected_state: Literal["hub", "unknown"]  # "hub": from the hub's config; "unknown": it has none
    devices: list[ExpectedDevice]
    buttons: list[ButtonRoute]
    note: str


def _notes(e: ExpectedState) -> list[str]:
    out = []
    if e.manual_power:
        out.append("powered by hand: the hub doesn't switch it")
    if e.relative_power:
        out.append("power is a toggle: the hub tracks it, the device may disagree")
    if e.relative_input:
        out.append("input is stepped through, not selected directly")
    return out


def describe_activity(hub: str, ref: str, catalog: Catalog, activity: Activity) -> ActivityDescription:
    """One activity: the devices it involves with their expected state, and where each button group goes."""
    devices: list[ExpectedDevice] = []
    for e in catalog.expected_states(activity):
        d = catalog.device_by_id(e.device_id)
        devices.append(
            {
                "name": d.name if d else str(e.device_id),
                "manufacturer": d.manufacturer if d else "",
                "model": d.model if d else "",
                "power": e.power,
                "input": e.input,
                "notes": _notes(e),
            }
        )
    buttons: list[ButtonRoute] = [
        {
            "group": group or "(no group)",
            "devices": [catalog.device_name(i) for i in ids],
            "commands": [c.name for c in activity.commands if c.group == group],
        }
        for group, ids in catalog.routes(activity)
    ]
    known = activity.expected is not None
    return {
        "hub": hub,
        "name": activity.name,
        "ref": ref,
        "expected_state": "hub" if known else "unknown",
        "devices": devices,
        "buttons": buttons,
        "note": ("Power and input as the hub's config gives them." if known else UNKNOWN_NOTE)
        + " The hub gives command names, not IR codes.",
    }


# --- the worksheet -----------------------------------------------------------------------
def _cell(value: object) -> str:
    """A Markdown table cell: pipes escaped and newlines flattened, so a name can't break the table."""
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip() or " "


def _row(*cells: object) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _device_label(d: Device) -> str:
    extra = " ".join(x for x in (d.manufacturer, d.model) if x)
    return f"{d.name} ({extra})" if extra else d.name


def worksheet(hub: str, catalog: Catalog, *, source: str, when: datetime) -> str:
    """The Markdown worksheet for one hub: devices, activities, then every device's commands."""
    lines = [
        f"# {hub}: Harmony setup worksheet",
        "",
        f"Exported from the Harmony hub {source} on {when:%Y-%m-%d %H:%M} UTC by mcp-server-harmony {__version__}.",
        "",
        f"> **What this is not.** {NOT_A_BACKUP}",
        ">",
        "> Expected power and input come from each activity's `fixit` data in the hub's config. "
        "Where it says unknown, the config had none for that activity: check the Harmony app.",
        "",
        "## Devices",
        "",
        _row("Device", "Manufacturer", "Model", "Commands"),
        _row("---", "---", "---", "---:"),
    ]
    lines += [_row(d.name, d.manufacturer, d.model, len(d.commands)) for d in catalog.devices]
    if not catalog.devices:
        lines.append("_(none)_")
    lines += ["", "## Activities", ""]
    if not catalog.activities:
        lines += ["_(none)_", ""]
    for a in catalog.activities:
        desc = describe_activity(hub, f"{hub}/{a.name}", catalog, a)
        lines += [f"### {a.name}", ""]
        if desc["expected_state"] == "unknown":
            lines += [f"Expected state: **unknown**. {UNKNOWN_NOTE}", ""]
        lines += [_row("Device", "Power", "Input", "Notes"), _row("---", "---", "---", "---")]
        for d in desc["devices"]:
            lines.append(_row(d["name"], d["power"], d["input"] or "", "; ".join(d["notes"])))
        if not desc["devices"]:
            lines.append(_row("(none)", "", "", ""))
        lines += ["", "Buttons:", "", _row("Button group", "Goes to", "Commands"), _row("---", "---", "---")]
        for b in desc["buttons"]:
            lines.append(_row(b["group"], ", ".join(b["devices"]), ", ".join(b["commands"])))
        if not desc["buttons"]:
            lines.append(_row("(none)", "", ""))
        lines.append("")
    lines += ["## Appendix: commands by device", ""]
    for dev in catalog.devices:
        lines += [f"### {_device_label(dev)}", ""]
        groups: dict[str, list[str]] = {}
        for c in dev.commands:
            groups.setdefault(c.group or "(no group)", []).append(c.name)
        if groups:
            lines += [_row("Group", "Commands"), _row("---", "---")]
            lines += [_row(g, ", ".join(names)) for g, names in groups.items()]
        else:
            lines.append("_No commands._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- export: one hub's files ---------------------------------------------------------------
def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-") or "hub"


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read (0600 on POSIX), even if it existed with wider permissions.

    os.open's mode applies only when the file is created, so an existing file is fchmod-ed before any
    content goes in. Windows has no POSIX modes; there the file inherits the folder's ACL.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


@dataclass
class HubExport:
    host: str
    name: str | None = None
    files: list[str] = field(default_factory=list)
    error: str | None = None


# (host, protocol) -> an object with connect/close/name/config/fw_version: HarmonyAPI, or a test fake.
ApiBuilder = Callable[[str, Any], Any]


async def export_hub(
    host: str,
    name: str | None,
    settings: Settings,
    out_dir: Path,
    *,
    redacted: bool,
    build: ApiBuilder,
    timeout: float,
    taken: set[str],
) -> HubExport:
    result = HubExport(host, name)
    api = build(host, settings.protocol or "WEBSOCKETS")
    try:
        try:
            ok = await asyncio.wait_for(api.connect(), timeout)
        except Exception as exc:  # noqa: BLE001 - whatever aioharmony raises, it's "couldn't connect"
            result.error = f"couldn't connect: {type(exc).__name__}: {exc}"
            return result
        if not ok:
            result.error = "couldn't connect (try `doctor`)"
            return result
        config = api.config or {}
        live = api.name if api.name and api.name != host else None
        hub = name or live or host
        result.name = hub
        base = slug = _slug(hub)
        n = 1
        while slug in taken:  # two hubs with one name still get a file each
            n += 1
            slug = f"{base}-{n}"
        taken.add(slug)
        when = datetime.now(UTC)
        doc = {
            "_meta": {
                "exported_at": when.isoformat(timespec="seconds"),
                "hub": hub,
                "host": host,
                "firmware": getattr(api, "fw_version", None),
                "exported_by": f"mcp-server-harmony {__version__}",
                "note": "The hub's config as it returned it, with Logitech account identifiers removed. "
                + NOT_A_BACKUP,
            },
            # scrub() is doctor --dump's: account keys, and any value with an e-mail address in it.
            "config": scrub(config),
        }
        text_json = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
        text_md = worksheet(
            hub,
            Catalog.from_config(config),
            source=f"{hub!r} at {host} (firmware {doc['_meta']['firmware']})",
            when=when,
        )
        if redacted:
            # For sharing: addresses masked the way logs and doctor --dump mask them.
            text_json, text_md = redact(text_json), redact(text_md)
        # A folder we create is the owner's only; one that exists (say --dir ~/Documents) is left as it is.
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for suffix, text in ((".json", text_json), (".md", text_md)):
            path = out_dir / f"{slug}{suffix}"
            write_private(path, text)
            result.files.append(str(path))
    finally:
        await api.close()
    return result
