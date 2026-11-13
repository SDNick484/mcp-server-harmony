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
_HW_2026_11_12 = "2026-11-12, Office Hub and Bedroom Hub (firmware 4.15.600), config read through this server"
_HW_2026_11_12_LIVE = (
    "2026-11-12, Office Hub and Bedroom Hub (firmware 4.15.600) driven through this server from a Claude session, "
    "with the owner at the TVs and the Harmony app; the Alpine service host for the history file"
)
_HW_2026_11_04 = (
    "2026-11-04, Office Hub and Bedroom Hub (firmware 4.15.600) driven through this server from a Claude session"
)

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
        status="hardware-verified",
        note="Partial, " + _HW_2026_10_14 + ": the running activity's id (32845636) resolved to its name "
        "through the parsed config. Also partial, " + _HW_2026_11_04 + ": commands resolved through the running "
        "activity's control groups and straight to named devices (an LG TV and an Onkyo receiver) and were "
        "accepted. Also, " + _HW_2026_11_12_LIVE + ": `doctor --dump` captures of both hubs (the first from "
        "firmware 4.15.600) are in tests/fixtures/recorded/ and replay through test_recorded.",
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
        status="hardware-contradicted",
        note=_HW_2026_11_04 + ": power_off(hub='Bedroom Hub', force=True) was sent while a start from the "
        "physical remote (SHIELD TV) was still running. The hub refused it: its reply msg was 'Activity in "
        "progress', which aioharmony returns as (False, msg) for any code other than 100 or 200, shown here as "
        "\"Bedroom Hub refused 'power off': Activity in progress\". The start then finished normally and a plain "
        "power_off afterwards worked. Matches the physical remote, where Off is ignored mid-start. One trial. "
        "The reply code and the interrupted start's own reply were not captured (no -v log). force still skips "
        "this server's own busy guard; whether it helps a hub that is truly stuck is untested.",
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
        status="hardware-verified",
        note=_HW_2026_11_04 + ": an Office Hub activity was renamed in the Harmony app (Office Xbox to Office "
        "Xbox 2) and list_activities showed the new name on the first call after the rename, with no restart of "
        "this server; renaming it back showed the old name the same way. Seen end to end only: the stateDigest "
        "frame with the new configVersion was not captured (no -v log) and the delay was not timed.",
    ),
    Assumption(
        "H-RECONNECT",
        "After the websocket drops, aioharmony reconnects by itself (first retry after 1 s, backing off to 30 s) "
        "and calls the disconnect and connect callbacks.",
        "aioharmony hubconnector_websocket._reconnect",
        "high",
        note="Partial, " + _HW_2026_11_04 + ": the Bedroom Hub was unplugged for a couple of minutes. get_status "
        "kept saying reachable: true for two polls, then reachable: false, then reachable: true about a minute "
        "after it was plugged back in, with no restart of this server. Not shown: the re-read after a reconnect "
        "(the hub was off throughout, so no state changed while it was away), the 1 s to 30 s retry timing, and "
        "the callbacks themselves. The Office Hub stayed reachable throughout and showed an activity started "
        "from its remote. Also, " + _HW_2026_11_12_LIVE + ": one history event arrived with noticed_late set "
        "although nobody unplugged a hub (cause unknown), so a late notice can happen, but it was not a "
        "controlled test. The re-read after a reconnect is still unexercised.",
    ),
    Assumption(
        "H-SSDP",
        "Hubs answer an SSDP M-SEARCH for ST urn:myharmony-com:device:harmony:1, with LOCATION on the hub's address.",
        "Home Assistant's harmony manifest matches that deviceType; HA may learn it from NOTIFY, not M-SEARCH",
        "medium",
    ),
    Assumption(
        "H-DIGITS",
        "A device with a tuner exposes digit commands named 0 to 9 in control group NumericBasic (plus '-' and "
        "sometimes '.' as the sub-channel separator), and pressing them in sequence, a gap apart, tunes the "
        "channel.",
        "how a physical remote works; the command names and an activity's routing of them were read from the hubs",
        "medium",
        status="hardware-verified",
        note=_HW_2026_11_12 + ": the LG TVs have '0' to '9' and '-' in NumericBasic (one also '.'), and the "
        "Bedroom Hub's 'Bedroom TV' activity routes them to the TV. Then " + _HW_2026_11_12_LIVE + ", on the "
        "Office Hub's LG only: the digits reached the TV whole at the 400 ms gap (its entry screen echoed "
        "5.1 and 5-1 as sent), the LG took '.' and '-' as the same sub-channel separator (both went from LG "
        "Channels to DTV 5-1), and '7' then OK (the select button) tuned channel 7 on LG Channels. Limits: a "
        "single digit without OK was inconsistent and unexplained (5 did nothing, 7 sometimes did not change "
        "the channel), the TV had no antenna signal, the fallback from '.' to '-' was not exercised, and the "
        "Bedroom Hub, favorites and any set-top box were not tried.",
    ),
    Assumption(
        "H-TIMING",
        "An activity start or power-off finishes within 60 s.",
        "none: our timeout, picked to cover slow TVs",
        "low",
    ),
    Assumption(
        "H-FIXIT",
        "Each activity in the config has a fixit object keyed by device id. Each entry has id, Power 'On' or "
        "'Off' (absent for a device powered by hand, which has isManualPower true instead), an optional Input "
        "(sometimes empty), and optional isRelativePower, isRelativeInput and isAlwaysOn flags. Devices the "
        "activity leaves off are listed with Power 'Off'.",
        "aioharmony never reads it. Published hub config dumps show it in this shape: vekkt0r/harmenubar "
        "unit/harmony.json, jfkriz/alexa-harmony-device-control controller.json, rkokkelk/com.jreenen.homeyharmonyhub "
        "example_json/activity.json; and the Fixit class of digitaldan/harmony-client (the library openHAB's "
        "Harmony binding uses) maps id, Power, Input, isAlwaysOn and isRelativePower. Those dumps are from older "
        "firmware; firmware 4.15.600 was unchecked until 2026-11-12",
        "medium",
        status="hardware-verified",
        note=_HW_2026_11_12_LIVE + ": both hubs' configs on 4.15.600 carry fixit on every activity, keyed by "
        "device id with Power and Input as described. describe_activity's expected power and input for "
        "Bedroom TV (Bedroom Hub) and Watch Office TV (Office Hub) matched the Harmony app (owner "
        "confirmed), and on the Office Hub the Onkyo (input tv) and the LG (Smart TV home) were found "
        "as fixit says. Open: on the Bedroom Hub the LG sat on HDMI 1 where fixit says Smart TV. fixit is "
        "the expected state in the config, not proof that the hub switches the input, so this is not "
        "counted as a contradiction. Only two activities were compared, not all nine.",
    ),
    Assumption(
        "H-ACTIVITY-PUSH",
        "When the running activity changes for any reason, including a start from the physical remote or the "
        "Harmony app, the hub pushes it over the websocket and aioharmony calls new_activity(id, name) without "
        "us asking. The history's 'elsewhere' records rest on this.",
        "aioharmony _notification_callback (stateDigest notify); the fake hub implements it",
        "medium",
        status="hardware-verified",
        note=_HW_2026_11_04 + ": activities were started from the Office Hub's and the Bedroom Hub's physical "
        "remotes (Office Xbox, then PS5; SHIELD TV) and get_status, which only reads the state we cache, showed "
        "them with no call from us to the hub, so they arrived as pushes. Starts only: a power off from the "
        "remote or app is H-OFF-PUSH. The frames and their timing were not captured (no -v log), so how soon "
        "after the change the push arrives, which `at` depends on, is unmeasured. Again, "
        + _HW_2026_11_12_LIVE
        + ": starts made with Alexa were followed with no call from us, and a start through this server gave "
        "exactly one history event.",
    ),
    Assumption(
        "H-OFF-PUSH",
        "A power off made from the remote or the Harmony app is pushed the same way, as new_activity with "
        "activity id -1, so history records it as a change to off.",
        "none: inferred from -1 being the PowerOff activity (H-POWEROFF) and H-ACTIVITY-PUSH",
        "low",
        status="hardware-verified",
        note=_HW_2026_11_12_LIVE + ": a power off from the Harmony app was recorded as a change with "
        "to_activity null and source 'elsewhere', and an app power off also dropped a pending sleep timer. "
        "Not tried: the remote's own Off button. The frames and their timing were not captured.",
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
