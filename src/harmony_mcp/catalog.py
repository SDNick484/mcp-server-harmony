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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

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


@dataclass(frozen=True)
class Activity:
    activity_id: int
    name: str
    commands: tuple[Command, ...] = field(default=(), repr=False)


def _norm(name: str) -> str:
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


def _find(commands: tuple[Command, ...], wanted: str) -> Command | None:
    key = _norm(wanted)
    for c in commands:
        if _norm(c.name) == key or _norm(c.label) == key:
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
            activities.append(Activity(aid, str(a.get("label", aid)), _parse_commands(a.get("controlGroup"))))
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
        key = _norm(name)
        return next((a for a in self.activities if _norm(a.name) == key), None)

    def activity_by_id(self, activity_id: int | None) -> Activity | None:
        return next((a for a in self.activities if a.activity_id == activity_id), None)

    def device(self, name: str) -> Device | None:
        key = _norm(name)
        return next((d for d in self.devices if _norm(d.name) == key), None)

    def device_name(self, device_id: int) -> str:
        d = next((d for d in self.devices if d.device_id == device_id), None)
        return d.name if d else str(device_id)

    @staticmethod
    def find_command(commands: tuple[Command, ...], name: str) -> Command | None:
        return _find(commands, name)
