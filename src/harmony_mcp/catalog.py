"""The hub's own configuration, turned into the names the model may use.

A Harmony hub already holds a closed world: the activities you set up, the
devices in them, and every command each device knows (the IR codes live on the
hub; we only ever send a command *name*). That makes the hub's config the
allow-list. The model can only start an activity, or send a command, that
appears here; there is no way to send a raw IR code or an unknown device id.

This module is pure data shaping (no I/O), so it is tested directly against a
config captured in the same shape the hub returns.

The raw config (``HarmonyAPI.config``) looks like this, trimmed::

    {
      "activity": [
        {"id": "-1", "label": "PowerOff", "controlGroup": []},
        {"id": "38123456", "label": "Watch Shield",
         "controlGroup": [
           {"name": "Volume", "function": [
             {"name": "VolumeUp", "label": "Volume Up",
              "action": "{\\"command\\":\\"VolumeUp\\",\\"type\\":\\"IRCommand\\",\\"deviceId\\":\\"71234567\\"}"}
           ]}
         ]}
      ],
      "device": [
        {"id": "71234567", "label": "Onkyo AV Receiver", "manufacturer": "Onkyo",
         "model": "TX-NR7100", "controlGroup": [...same shape as above...]}
      ]
    }

Two quirks worth knowing: ids are *strings* in the config but *ints* everywhere
in aioharmony's API, and each function's ``action`` is itself a JSON string.

Each activity also has a ``fixit`` object: what the activity expects of every
device on the hub, keyed by device id (ASSUMPTION H-FIXIT; aioharmony never
reads it, so this shape comes from other projects' published config dumps)::

    "fixit": {
      "71234567": {"id": "71234567", "Power": "On", "Input": "HDMI 1", "isRelativePower": true},
      "71234568": {"id": "71234568", "Power": "Off"},
      "71234569": {"id": "71234569", "isManualPower": true}
    }

It is parsed into ``Activity.expected``, and only ever *reported* (the
describe tool, ``export``): nothing is sent because of it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

# Harmony's built-in "everything off" pseudo-activity. It is in the config's
# activity list, but it is not something the user set up, so it is reported as
# power "off" rather than listed as an activity.
POWER_OFF_ID = -1


@dataclass(frozen=True)
class Command:
    """One button the hub knows how to send.

    name      - the hub's canonical name, e.g. "VolumeUp". This is what is sent.
    label     - the human label, e.g. "Volume Up". Accepted as input too.
    device_id - the device that receives it. For an activity's commands this is
                whichever device the activity routes that button to (volume to
                the receiver, play/pause to the Shield, ...).
    group     - the hub's control group, e.g. "Volume", "TransportBasic".
    """

    name: str
    label: str
    device_id: int
    group: str


@dataclass(frozen=True)
class Device:
    device_id: int
    name: str
    manufacturer: str
    model: str
    commands: tuple[Command, ...] = field(default=(), repr=False)


Power = Literal["on", "off", "unknown"]


@dataclass(frozen=True)
class ExpectedState:
    """What an activity expects of one device, from the activity's ``fixit`` entry.

    power          - "on" or "off" as the hub says, or "unknown" when the entry has no
                     usable Power (a device powered by hand has none at all).
    input          - the input the activity selects, e.g. "HDMI 1"; None when the entry
                     names none (or names an empty one, which real dumps do).
    manual_power   - isManualPower: the hub doesn't switch this device; you do.
    relative_power - isRelativePower: power is a toggle, so the hub *tracks* on/off
                     rather than setting it, and can be out of step with the device.
    relative_input - isRelativeInput: the input is reached by stepping through inputs,
                     with the same risk.
    """

    device_id: int
    power: Power
    input: str | None = None
    manual_power: bool = False
    relative_power: bool = False
    relative_input: bool = False


@dataclass(frozen=True)
class Activity:
    activity_id: int
    name: str
    commands: tuple[Command, ...] = field(default=(), repr=False)
    # None means "unknown": the activity had no fixit, or one we couldn't read. An empty
    # tuple never stands for that, so "unknown" can't be mistaken for "involves nothing".
    expected: tuple[ExpectedState, ...] | None = field(default=None, repr=False)


def norm(name: str) -> str:
    """Matching key: case-, space-, dash- and underscore-insensitive.

    The model may say "volume up", "Volume Up", "VolumeUp" or "volume_up";
    all of them should find the hub's "VolumeUp".
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _parse_commands(control_groups: Any) -> tuple[Command, ...]:
    out: list[Command] = []
    for group in control_groups or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name", ""))
        for fn in group.get("function", []) or []:
            if not isinstance(fn, dict):
                continue
            try:
                action = json.loads(fn.get("action") or "")
                cmd = Command(
                    name=str(action["command"]),
                    label=str(fn.get("label") or fn.get("name") or action["command"]),
                    device_id=int(action["deviceId"]),
                    group=group_name,
                )
            except (ValueError, KeyError, TypeError):
                # A function we can't parse is simply not offered to the model.
                log.debug("Skipping unparseable function %r", fn)
                continue
            out.append(cmd)
    return tuple(out)


def _flag(value: Any) -> bool:
    # The real dumps use JSON booleans; a "true" string is accepted too, the way the device's
    # own isManualPower is a string ("false") in the same dumps.
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _parse_fixit(raw: Any) -> tuple[ExpectedState, ...] | None:
    """An activity's fixit, tolerantly. None when it is missing or not readable at all.

    One bad entry is skipped rather than spoiling the rest: the devices it would have
    described simply come out as "unknown" wherever they are reported.
    """
    # ASSUMPTION H-FIXIT: an object keyed by device id, entries with Power/Input and flags.
    if not isinstance(raw, dict):
        return None
    out: list[ExpectedState] = []
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            log.debug("Skipping unreadable fixit entry %r", entry)
            continue
        raw_id: Any = entry.get("id", key)
        try:
            device_id = int(raw_id)
        except (TypeError, ValueError):
            log.debug("Skipping fixit entry with no usable id %r", entry)
            continue
        # Exactly "On" or "Off"; anything else (absent for a hand-powered device) stays "unknown".
        power_raw = entry.get("Power")
        power: Power = "on" if power_raw == "On" else "off" if power_raw == "Off" else "unknown"
        input_raw = entry.get("Input")
        out.append(
            ExpectedState(
                device_id,
                power,
                input_raw.strip() if isinstance(input_raw, str) and input_raw.strip() else None,
                manual_power=_flag(entry.get("isManualPower")),
                relative_power=_flag(entry.get("isRelativePower")),
                relative_input=_flag(entry.get("isRelativeInput")),
            )
        )
    return tuple(out) if out else None


def _find(commands: tuple[Command, ...], wanted: str) -> Command | None:
    key = norm(wanted)
    for c in commands:
        if norm(c.name) == key or norm(c.label) == key:
            return c
    return None


@dataclass(frozen=True)
class Catalog:
    """Everything the hub's config says the model is allowed to touch."""

    activities: tuple[Activity, ...] = ()
    devices: tuple[Device, ...] = ()

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> Catalog:
        if not config:
            return cls()
        activities = []
        for a in config.get("activity", []) or []:
            try:
                aid = int(a["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if aid == POWER_OFF_ID:
                continue
            activities.append(
                Activity(
                    aid,
                    str(a.get("label", aid)),
                    _parse_commands(a.get("controlGroup")),
                    _parse_fixit(a.get("fixit")),
                )
            )
        devices = []
        for d in config.get("device", []) or []:
            try:
                did = int(d["id"])
            except (KeyError, TypeError, ValueError):
                continue
            devices.append(
                Device(
                    did,
                    str(d.get("label", did)),
                    str(d.get("manufacturer", "")),
                    str(d.get("model", "")),
                    _parse_commands(d.get("controlGroup")),
                )
            )
        return cls(tuple(activities), tuple(devices))

    # --- lookups by what the model typed ----------------------------------
    def activity(self, name: str) -> Activity | None:
        key = norm(name)
        return next((a for a in self.activities if norm(a.name) == key), None)

    def activity_by_id(self, activity_id: int | None) -> Activity | None:
        return next((a for a in self.activities if a.activity_id == activity_id), None)

    def device(self, name: str) -> Device | None:
        key = norm(name)
        return next((d for d in self.devices if norm(d.name) == key), None)

    def device_by_id(self, device_id: int) -> Device | None:
        return next((d for d in self.devices if d.device_id == device_id), None)

    def device_name(self, device_id: int) -> str:
        d = self.device_by_id(device_id)
        return d.name if d else str(device_id)

    # --- what an activity does, for describe and export ---------------------------
    @staticmethod
    def routes(activity: Activity) -> tuple[tuple[str, tuple[int, ...]], ...]:
        """Each control group of an activity, in the hub's order, with the device(s) its buttons go to.

        Usually one device per group (Volume to the receiver, TransportBasic to the
        streamer), but nothing stops a group mixing devices, so it's a tuple.
        """
        groups: dict[str, dict[int, None]] = {}  # dicts as ordered sets
        for c in activity.commands:
            groups.setdefault(c.group, {})[c.device_id] = None
        return tuple((g, tuple(ids)) for g, ids in groups.items())

    def expected_states(self, activity: Activity) -> tuple[ExpectedState, ...]:
        """Every device the activity involves, with its expected state.

        From the hub's fixit when there is one, plus any device the activity's buttons
        route to that fixit left out ("unknown", not assumed on). Without a fixit, only
        the routed devices, all "unknown". Ordered like the hub's device list.
        """
        known = {e.device_id: e for e in activity.expected or ()}
        routed = {c.device_id for c in activity.commands}
        ids = set(known) | routed
        order = {d.device_id: i for i, d in enumerate(self.devices)}
        return tuple(
            known.get(i, ExpectedState(i, "unknown")) for i in sorted(ids, key=lambda i: (order.get(i, len(order)), i))
        )

    @staticmethod
    def find_command(commands: tuple[Command, ...], name: str) -> Command | None:
        return _find(commands, name)
