"""Every protocol detail this server relies on that hasn't been checked on a real hub.

Why a registry instead of comments: a comment saying "unverified" is easy to
miss and never goes away. Here each assumption has an id that:
  - code cites next to where it depends on it (``# ASSUMPTION H-PROVISION``),
  - a test cites to document it (tests/test_assumptions.py, test_contract.py),
  - HARDWARE_VALIDATION.md cites in the step that confirms it,
  - `mcp-server-harmony doctor` prints, with its status.

tests/test_assumptions.py fails if an id is missing from the README or
HARDWARE_VALIDATION.md, so the three can't drift apart.

To record a hardware result, change ``status`` to "hardware-verified" (or
"hardware-contradicted", with what you saw in ``note``) and commit it.

Only the list is about Harmony. The record (``Assumption``), its statuses and the
queries come from the shared ``registry.py`` (see its docstring for what each status
means), so a fix there reaches every sibling server.

Confidence is about the *claim*, judged from its source:
  high   - the library we run (aioharmony, used by Home Assistant) depends on it
  medium - inferred from another project's code or docs, not from aioharmony itself
  low    - our own guess; the simulator implements it but nothing confirms it
"""

from __future__ import annotations

from collections.abc import Iterable

from .registry import Assumption, by_id, doctor_lines
from .registry import contradicted as _contradicted
from .registry import unverified as _unverified

__all__ = ["ASSUMPTIONS", "BY_ID", "Assumption", "contradicted", "doctor_lines", "unverified"]

# Hardware runs, cited by the notes below.
_HW_2026_10_14 = "2026-10-14, two real hubs (Harmony Hub, Office Hub, firmware 4.15.600) via the service on LXC210"

ASSUMPTIONS: tuple[Assumption, ...] = (
    Assumption(
        "H-PORT",
        "The hub's local API is HTTP and websocket on TCP 8088.",
        "aioharmony const.DEFAULT_WS_HUB_PORT, used for every request",
        "high",
        status="hardware-verified",
        note=_HW_2026_10_14 + ": both hubs connected on 8088.",
    ),
    Assumption(
        "H-PROVISION",
        "POST http://<hub>:8088/ with cmd 'setup.account?getProvisionInfo' returns data.activeRemoteId, "
        "which the websocket URL needs as hubId.",
        "aioharmony hubconnector_websocket._retrieve_hub_info; Home Assistant validates hubs the same way",
        "high",
        status="hardware-verified",
        note=_HW_2026_10_14 + ": both websockets opened, which needs the provisioned hubId.",
    ),
    Assumption(
        "H-FRAMES",
        "Requests are {hubId, timeout, hbus:{cmd, id, params}}; replies are {cmd, code, id, msg, data} and are "
        "matched to requests by id.",
        "aioharmony hub_send and responsehandler",
        "high",
        status="hardware-verified",
        note=_HW_2026_10_14 + ": get_status and power_off got their replies.",
    ),
    Assumption(
        "H-CONFIG-SHAPE",
        "The config has activity[] and device[] with string ids, controlGroup[].function[], and each "
        "function's action is a JSON *string* holding command and deviceId.",
        "aioharmony json_config and _get_config",
        "high",
        note="Partial, " + _HW_2026_10_14 + ": the running activity's id (32845636) resolved to its name "
        "through the parsed config. Devices and commands not exercised yet.",
    ),
    Assumption(
        "H-POWEROFF",
        "Activity -1 is the built-in PowerOff; starting it turns everything off.",
        "aioharmony power_off() is start_activity(-1)",
        "high",
        status="hardware-verified",
        note=_HW_2026_10_14 + ": power_off on Office Hub sent runactivity -1; the hub went off, and the LG "
        "Office TV dropped to standby (confirmed independently by mcp-server-webos).",
    ),
    Assumption(
        "H-NAME",
        "'connect.discoveryinfo?get' returns the hub's friendlyName (the name set in the Harmony app).",
        "aioharmony HarmonyClient.name",
        "high",
        status="hardware-verified",
        note=_HW_2026_10_14 + ": the hubs reported 'Harmony Hub' and 'Office Hub'.",
    ),
    Assumption(
        "H-START-SEQUENCE",
        "Starting an activity: the hub acks runactivity, sends stateDigest notify (activityStatus 1, or 0 for "
        "power off), startActivity code 100 progress frames, startActivity code 200 when done, then "
        "startActivityFinished.",
        "aioharmony's start_activity handlers; the fake hub's exact order and timing are hand-built",
        "medium",
    ),
    Assumption(
        "H-FORCE-OFF",
        "runactivity -1 sent while another activity's start sequence is running interrupts it: the hub stops "
        "that sequence, runs its power off, and ends on PowerOff (-1). The interrupted start's own request may "
        "then get a late reply, success or failure.",
        "none: aioharmony allows the overlap (each runactivity has its own msgid, no lock); the fake hub just "
        "runs the power off",
        "low",
    ),
    Assumption(
        "H-PRESS-SILENT",
        "holdAction press/release gets no reply on success, only on failure.",
        "aioharmony send_commands comment ('the HUB sends a message back if there is an issue')",
        "medium",
    ),
    Assumption(
        "H-PRESS-ERROR",
        "A rejected press is answered with a non-200 code and a msg. The fake hub uses code 417 with "
        "'Unknown command'; the real code and text are unknown.",
        "none: the fake hub's choice",
        "low",
    ),
    Assumption(
        "H-HOLD",
        "hold_ms keeps the button 'pressed' between the press and release frames; whether the hub repeats the "
        "IR code while held (like a held physical button) is unknown.",
        "aioharmony SendCommandDevice.delay (press, sleep, release)",
        "low",
    ),
    Assumption(
        "H-CONFIG-PUSH",
        "Editing the hub in the Harmony app makes it push stateDigest notify with a new configVersion "
        "(syncStatus not 1); aioharmony then refetches the config.",
        "aioharmony _notification_callback",
        "medium",
    ),
    Assumption(
        "H-RECONNECT",
        "After the websocket drops, aioharmony reconnects by itself (first retry after 1 s, backing off to 30 s) "
        "and calls the disconnect and connect callbacks.",
        "aioharmony hubconnector_websocket._reconnect",
        "high",
    ),
    Assumption(
        "H-SSDP",
        "Hubs answer an SSDP M-SEARCH for ST urn:myharmony-com:device:harmony:1, with LOCATION on the hub's address.",
        "Home Assistant's harmony manifest matches that deviceType; HA may learn it from NOTIFY, not M-SEARCH",
        "medium",
    ),
    Assumption(
        "H-TIMING",
        "An activity start or power-off finishes within 60 s.",
        "none: our timeout, picked to cover slow TVs",
        "low",
    ),
)

# by_id, not a dict comprehension: a repeated id fails at import instead of silently dropping an entry.
BY_ID = by_id(ASSUMPTIONS)


# The shared queries take the list as an argument (they serve every sibling); these default it to ours.
def unverified(assumptions: Iterable[Assumption] = ASSUMPTIONS) -> list[Assumption]:
    """Still ``simulator-only``. A contradicted assumption was checked, so it isn't counted here."""
    return _unverified(assumptions)


def contradicted(assumptions: Iterable[Assumption] = ASSUMPTIONS) -> list[Assumption]:
    """Seen wrong on hardware (``hardware-contradicted``); the code was changed to stop relying on them."""
    return _contradicted(assumptions)
