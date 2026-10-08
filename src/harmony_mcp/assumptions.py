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

Confidence is about the *claim*, judged from its source:
  high   - the library we run (aioharmony, used by Home Assistant) depends on it
  medium - inferred from another project's code or docs, not from aioharmony itself
  low    - our own guess; the simulator implements it but nothing confirms it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]


@dataclass(frozen=True)
class Assumption:
    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""


ASSUMPTIONS: tuple[Assumption, ...] = (
    Assumption(
        "H-PORT",
        "The hub's local API is HTTP and websocket on TCP 8088.",
        "aioharmony const.DEFAULT_WS_HUB_PORT, used for every request",
        "high",
    ),
    Assumption(
        "H-PROVISION",
        "POST http://<hub>:8088/ with cmd 'setup.account?getProvisionInfo' returns data.activeRemoteId, "
        "which the websocket URL needs as hubId.",
        "aioharmony hubconnector_websocket._retrieve_hub_info; Home Assistant validates hubs the same way",
        "high",
    ),
    Assumption(
        "H-FRAMES",
        "Requests are {hubId, timeout, hbus:{cmd, id, params}}; replies are {cmd, code, id, msg, data} and are "
        "matched to requests by id.",
        "aioharmony hub_send and responsehandler",
        "high",
    ),
    Assumption(
        "H-CONFIG-SHAPE",
        "The config has activity[] and device[] with string ids, controlGroup[].function[], and each "
        "function's action is a JSON *string* holding command and deviceId.",
        "aioharmony json_config and _get_config",
        "high",
    ),
    Assumption(
        "H-POWEROFF",
        "Activity -1 is the built-in PowerOff; starting it turns everything off.",
        "aioharmony power_off() is start_activity(-1)",
        "high",
    ),
    Assumption(
        "H-NAME",
        "'connect.discoveryinfo?get' returns the hub's friendlyName (the name set in the Harmony app).",
        "aioharmony HarmonyClient.name",
        "high",
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

BY_ID = {a.id: a for a in ASSUMPTIONS}


def unverified() -> list[Assumption]:
    return [a for a in ASSUMPTIONS if a.status != "hardware-verified"]
