"""MCP tool definitions.

The tool surface is *activity-first*, because that's how a Harmony is meant to
be used and what makes an LLM good at it: "watch the Shield" is one call to
start_activity, and the hub handles powering devices and switching inputs. For
buttons, send_command without a device goes through the running activity, so
"volume up" reaches whatever the activity routes volume to (the receiver, the
TV) without the model having to know your wiring.

How a decorated function becomes a tool (same as the sibling projects):
  - the docstring is the tool's description, written for the model;
  - the signature becomes the inputSchema (Field(ge=, le=) -> minimum/maximum);
  - a TypedDict return type becomes the outputSchema, sent as structured content.

What bounds the model is the hub's own config (see catalog.py): every name is
resolved against it, and only commands the hub already knows can be sent.
limits.py adds per-call caps and per-hub rate limits on top.

One server can drive several hubs (one per TV). Every tool takes an optional
``hub``; without it, names are looked up across all hubs and a name found on
more than one is an error that lists them (see hubs.py). Names may also be
hub-qualified, "Den/Watch TV", which is what the list tools return as ``ref``.

Why there is no tool that returns a hub's raw config: it is tens of kilobytes the
model has no use for. harmony_describe_activity gives the useful part of it
(what an activity turns on, which input, where each button goes), and
`mcp-server-harmony export` writes the whole thing to a file.

Why there is no separate list_hubs tool: get_status already lists every hub
with its state, and two tools that both "list hubs" split the model's choice
for no gain.

Status: verified against the simulator only (see assumptions.py).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal, NotRequired
from urllib.parse import unquote

from mcp.server import MCPServer
from pydantic import Field
from typing_extensions import TypedDict

from . import __version__
from .catalog import Catalog, Command
from .channels import SEPARATORS, looks_like_number, number_problem
from .client import HarmonyError, HubClient, Outcome, SleepTimerInfo, Status
from .config import load_settings
from .export import ActivityDescription, describe_activity
from .history import HistoryEvent
from .hubs import ALL, NO_HUBS, HubRegistry, ref
from .limits import (
    MAX_CHANNEL_ARG,
    MAX_DELAY_MS,
    MAX_HOLD_MS,
    MAX_REPEAT,
    MAX_SLEEP_MINUTES,
    MIN_DELAY_MS,
)
from .lookup import unknown_with_suggestions
from .tools import ACT, ACT_IDEMPOTENT, READ_ONLY

log = logging.getLogger(__name__)

_hubs: HubRegistry | None = None
# `serve` records activity history. A one-shot `call` sets this False before it starts the server in
# process: it only reads the history file (history.py explains why). A module flag, not a tool argument:
# the model must never be able to switch recording off.
record_history = True


def hubs() -> HubRegistry:
    assert _hubs is not None, "server lifespan has not started"
    return _hubs


async def ready_hubs() -> HubRegistry:
    """hubs(), but the first tool call after startup waits briefly for hubs still connecting."""
    r = hubs()
    await r.ready()
    return r


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    """Open every hub connection once at startup; close them at shutdown."""
    global _hubs
    _hubs = HubRegistry(load_settings())
    # Before start(): that is where the first state read would be recorded. The file was already loaded by the
    # constructor, so a read-only history still has what the real server wrote.
    _hubs.history.read_only = not record_history
    await _hubs.start()
    try:
        yield
    finally:
        await _hubs.stop()
        _hubs = None


mcp = MCPServer(
    "harmony",
    version=__version__,  # serverInfo.version in initialize, so a bug report says which build answered
    instructions=(
        "Controls Logitech Harmony Hubs, usually one per TV. Call harmony_get_status first: it lists each hub by name, "
        "whether it is on, and its running activity. Prefer activities: harmony_start_activity (e.g. 'Watch Shield') "
        "powers the right devices and switches inputs; harmony_power_off turns a hub's devices off. For buttons, "
        "harmony_send_command without a device goes through the running activity. Names can be hub-qualified "
        "('Den/Watch TV', the `ref` field of list results); pass hub only when a name exists on several hubs."
    ),
    lifespan=lifespan,
)

# Annotations from .tools: explicit on every tool, since a tool without them is
# assumed destructive, non-idempotent and open-world.
_READ = READ_ONLY
_ACT = ACT
_ACT_IDEMPOTENT = ACT_IDEMPOTENT

Hub = Annotated[
    str | None,
    Field(
        description="Hub name from harmony_get_status. Omit to look across all hubs (needed only if a name repeats)."
    ),
]


# --- result shapes (each becomes an outputSchema) ---------------------------------------
class HubsStatus(TypedDict):
    dry_run: bool  # true: writes are reported, not sent
    hubs: list[Status]
    hint: NotRequired[str]  # only with no hub configured: how to add one


class ActivityInfo(TypedDict):
    hub: str
    name: str
    ref: str  # "Den/Watch TV": accepted anywhere an activity name is
    running: bool


class DeviceInfo(TypedDict):
    hub: str
    name: str
    ref: str
    manufacturer: str
    model: str
    commands: int


class CommandInfo(TypedDict):
    name: str
    label: str
    group: str
    device: str  # the device that actually receives it


class CommandList(TypedDict):
    hub: str
    source: str  # "activity Watch Shield" or "device Onkyo AV Receiver"
    commands: list[CommandInfo]


class ActionResult(TypedDict):
    hub: str
    outcome: Literal["done", "unchanged", "dry_run", "error"]
    detail: str  # one sentence for the user
    sent: list[str]  # protocol frames sent (or, in dry-run, that would have been)


class PowerOffResult(TypedDict):
    results: list[ActionResult]


class SleepTimerResult(TypedDict):
    hub: str
    outcome: Literal["set", "cancelled"]
    detail: str  # one sentence for the user, including where the timer lives
    sleep_timer: SleepTimerInfo | None  # the pending timer; null after a cancel


class HistoryPage(TypedDict):
    enabled: bool  # false: history is turned off, nothing is recorded
    persisted: bool  # true: events are also written to history.jsonl and survive a restart
    session_started: str  # when this server process began recording (UTC)
    recording_since: str | None  # the oldest record still kept; nothing earlier is known
    last_seq: int  # pass as after_seq next time to get only what's new
    expired: bool  # after_seq no longer lines up (older records were dropped, or the counter restarted)
    events: list[HistoryEvent]
    hint: NotRequired[str]


def _commands_view(catalog: Catalog, commands: tuple[Command, ...]) -> list[CommandInfo]:
    return [
        {"name": c.name, "label": c.label, "group": c.group, "device": catalog.device_name(c.device_id)}
        for c in commands
    ]


def _result(h: HubClient, o: Outcome, done: str, unchanged: str) -> ActionResult:
    if o.dry_run:
        return {"hub": h.name, "outcome": "dry_run", "detail": f"DRY RUN, nothing sent: would {done}", "sent": o.sent}
    if o.changed:
        return {"hub": h.name, "outcome": "done", "detail": done[0].upper() + done[1:], "sent": o.sent}
    return {"hub": h.name, "outcome": "unchanged", "detail": unchanged, "sent": []}


# --- reads ------------------------------------------------------------------------------
@mcp.tool(title="Get hub status", annotations=_READ)
async def harmony_get_status() -> HubsStatus:
    """Start here. Lists every hub (one per TV) with whether it's reachable, on or off, and its running activity.

    transition is non-null while an activity is starting or powering off; that hub's commands wait until it
    clears. dry_run true means this server reports writes instead of sending them.

    sleep_timer is a pending harmony_set_sleep_timer (when it fires, minutes left) or null. last_sleep_timer says how
    the previous one ended (fired, cancelled or failed, and why): nothing else tells you when a timer fires, so
    check here after the time has passed.
    """
    r = await ready_hubs()
    status: HubsStatus = {"dry_run": r.settings.dry_run, "hubs": [h.snapshot() for h in r.hubs]}
    if not r.hubs:
        # A first run: an empty list alone reads as "you have no hubs", so say how to add one.
        status["hint"] = NO_HUBS
    return status


@mcp.tool(title="List activities", annotations=_READ)
async def harmony_list_activities(hub: Hub = None) -> list[ActivityInfo]:
    """List the activities on each hub (e.g. 'Watch Shield'), marking the running ones. Each has a ref usable
    anywhere an activity name is."""
    return [
        {"hub": h.name, "name": a.name, "ref": ref(h, a.name), "running": a.activity_id == h.activity_id}
        for h in (await ready_hubs()).candidates(hub)
        for a in h.catalog.activities
    ]


@mcp.tool(title="List devices", annotations=_READ)
async def harmony_list_devices(hub: Hub = None) -> list[DeviceInfo]:
    """List the devices each hub controls, with how many commands each knows. Use only when an activity
    doesn't cover what you need (harmony_send_command with device)."""
    return [
        {
            "hub": h.name,
            "name": d.name,
            "ref": ref(h, d.name),
            "manufacturer": d.manufacturer,
            "model": d.model,
            "commands": len(d.commands),
        }
        for h in (await ready_hubs()).candidates(hub)
        for d in h.catalog.devices
    ]


Target = Annotated[
    str | None,
    Field(description="Activity or device name or ref. Omit for the running activity's buttons."),
]


@mcp.tool(title="List commands", annotations=_READ)
async def harmony_list_commands(target: Target = None, hub: Hub = None) -> CommandList:
    """List the commands harmony_send_command accepts for an activity or device (default: the running activity).

    Each entry says which device actually receives it. Activities expose the buttons they route; devices
    expose everything they know.
    """
    h, source, commands = (await ready_hubs()).commands_for(target, hub)
    return {"hub": h.name, "source": source, "commands": _commands_view(h.catalog, commands)}


# --- writes -----------------------------------------------------------------------------
@mcp.tool(title="Start an activity", annotations=_ACT_IDEMPOTENT)
async def harmony_start_activity(
    activity: Annotated[
        str, Field(description="Activity name or ref from harmony_list_activities, e.g. 'Den/Watch TV'.")
    ],
    hub: Hub = None,
) -> ActionResult:
    """Start an activity: its hub powers on the devices and sets their inputs, then this returns (a few seconds).

    Starting the activity that is already running does nothing (outcome 'unchanged'). Switching activities on
    the same hub needs no harmony_power_off first.
    """
    h, a = (await ready_hubs()).activity(activity, hub)
    outcome = await h.start_activity(a)
    return _result(h, outcome, f"start {a.name} on {h.name}", f"{a.name} was already running on {h.name}")


@mcp.tool(title="Turn a TV system off", annotations=_ACT_IDEMPOTENT)
async def harmony_power_off(
    hub: Annotated[
        str | None,
        Field(description=f"Hub name, or '{ALL}' for every hub. Omit when only one hub is on."),
    ] = None,
    force: Annotated[bool, Field(description="Force power-off even if an activity sequence is in progress.")] = False,
) -> PowerOffResult:
    """Run a hub's power-off, turning off every device in its running activity.

    With no hub and several hubs on, this asks which; hub='all' turns off every one and reports each hub
    separately (one unreachable hub doesn't stop the others).

    force=True is for a hub stuck mid-sequence (harmony_get_status shows a transition that never clears): it sends
    the power off anyway. Use it only after a normal harmony_power_off was refused as busy. Without hub it acts on
    hubs that are on or mid-sequence; to force one that reports off, name it.
    """
    targets = (await ready_hubs()).to_power_off(hub)
    results: list[ActionResult] = []
    for h in targets:
        try:
            outcome = await h.power_off(force=force)
            results.append(_result(h, outcome, f"power off {h.name}", f"{h.name} was already off"))
        except HarmonyError as exc:
            if len(targets) == 1:
                raise
            results.append({"hub": h.name, "outcome": "error", "detail": str(exc), "sent": []})
    if results and all(r["outcome"] == "error" for r in results):
        raise HarmonyError("; ".join(f"{r['hub']}: {r['detail']}" for r in results))
    return {"results": results}


@mcp.tool(title="Send a command", annotations=_ACT)
async def harmony_send_command(
    command: Annotated[str, Field(description="Command name or label from harmony_list_commands, e.g. 'VolumeUp'.")],
    device: Annotated[
        str | None,
        Field(description="Send straight to this device (name or ref). Omit to send through the running activity."),
    ] = None,
    hub: Hub = None,
    repeat: Annotated[int, Field(ge=1, le=MAX_REPEAT, description=f"Presses, 1-{MAX_REPEAT}.")] = 1,
    hold_ms: Annotated[
        int, Field(ge=0, le=MAX_HOLD_MS, description="Hold each press this long (a long press). 0 is a tap.")
    ] = 0,
    delay_ms: Annotated[
        int | None,
        Field(ge=MIN_DELAY_MS, le=MAX_DELAY_MS, description="Pause between repeats (default 400 ms)."),
    ] = None,
) -> ActionResult:
    """Press a button, e.g. command='VolumeUp' with repeat=3, or command='Pause'.

    Without device, the running activity decides which device gets it. Names come from harmony_list_commands;
    there is no way to send a raw IR code. Rapid repeated calls are rate-limited per hub.
    """
    r = await ready_hubs()
    if device is None:
        h, act = r.running(hub)
        pool, where = act.commands, f"activity {act.name}"
    else:
        h, dev = r.device(device, hub)
        pool, where = dev.commands, f"device {dev.name}"
    cmd = Catalog.find_command(pool, command)
    if cmd is None:
        raise HarmonyError(
            unknown_with_suggestions(
                f"{where} on {h.name}",
                command,
                [c.name for c in pool],
                fallback="Use harmony_list_commands to see what it accepts.",
            )
        )
    outcome = await h.send(cmd, repeat=repeat, hold_ms=hold_ms, delay_ms=delay_ms)
    target = h.catalog.device_name(cmd.device_id)
    return _result(h, outcome, f"send {cmd.name} x{repeat} to {target} on {h.name}", "")


@mcp.tool(title="Describe an activity", annotations=_READ)
async def harmony_describe_activity(
    activity: Annotated[
        str, Field(description="Activity name or ref from harmony_list_activities, e.g. 'Den/Watch TV'.")
    ],
    hub: Hub = None,
) -> ActivityDescription:
    """What an activity sets up, read from the hub's config; sends nothing.

    Lists each device it involves with its expected power (on, off or unknown) and input, and which device
    each group of buttons goes to (e.g. Volume to the receiver). expected_state 'unknown' means the hub's
    config doesn't say: report it as unknown, don't guess. Useful to rebuild an activity on another remote.
    """
    h, a = (await ready_hubs()).activity(activity, hub)
    return describe_activity(h.name, ref(h, a.name), h.catalog, a)


@mcp.tool(title="Set a sleep timer", annotations=_ACT)
async def harmony_set_sleep_timer(
    minutes: Annotated[
        int,
        Field(ge=0, le=MAX_SLEEP_MINUTES, description=f"Minutes from now, 1-{MAX_SLEEP_MINUTES}. 0 cancels the timer."),
    ],
    hub: Annotated[
        str | None,
        Field(description="Hub name from harmony_get_status. Omit when only one hub is on."),
    ] = None,
) -> SleepTimerResult:
    """Turn a hub's TV system off in `minutes` minutes (the same power-off as harmony_power_off); 0 cancels.

    "Turn the bedroom TV off in 45 minutes" is one call. A new timer replaces the one already set on that hub.
    With no hub and several hubs on, this asks which. A hub that is off is refused.

    The timer is held in this server's memory, not on the hub, and is not saved: under `serve --http` it outlives
    the chat, but over stdio the server exits when the MCP client closes, and the timer goes with it. It is
    dropped if the hub is powered off first (by anyone); switching activities keeps it. If the hub is busy or
    unreachable when it fires, it retries for a few minutes, then gives up. Nothing notifies you when it fires:
    check harmony_get_status (sleep_timer while pending, last_sleep_timer afterwards).
    """
    h = (await ready_hubs()).sleep_target(hub, cancel=minutes == 0)
    if minutes == 0:
        if await h.cancel_sleep_timer() is None:
            raise HarmonyError(f"No sleep timer is set on {h.name}.")
        return {
            "hub": h.name,
            "outcome": "cancelled",
            "detail": f"Cancelled the sleep timer on {h.name}.",
            "sleep_timer": None,
        }
    timer, replaced = await h.set_sleep_timer(minutes)
    detail = (
        f"{h.name} will be turned off in {minutes} minute{'s' if minutes != 1 else ''} (at {timer['fires_at']} UTC)."
        + (" This replaced the timer that was already set." if replaced else "")
        + " The timer is held in this server's memory only: under `serve --http` it outlives the chat, but over "
        "stdio it is lost when the MCP client closes the server. Check harmony_get_status afterwards."
    )
    if h.dry_run:
        detail += " DRY RUN: when it fires the power off will be reported, not sent."
    return {"hub": h.name, "outcome": "set", "detail": detail, "sleep_timer": timer}


# The buttons that confirm a typed number, in the order tried. The name varies by device: the LG TVs call
# it OK (label Select), the Onkyo Enter. Looked up in the pool like any command, never sent by guess.
SELECT_BUTTONS = ("OK", "Select", "Enter")


def _digit_buttons(pool: tuple[Command, ...], number: str) -> tuple[list[Command], list[str]]:
    """The presses for `number` and what the pool lacks to type it: ([commands], [missing, ...]).

    A separator the pool doesn't have falls back to the other one: the "-" and "." on a remote both mean
    "the sub-channel comes next", and which one a TV has varies (the Bedroom TV has only "-"). Resolving
    every press before sending any is what keeps a number from being half-typed.
    """
    presses: list[Command] = []
    missing: list[str] = []
    for ch in number:
        wanted = [ch] if ch not in SEPARATORS else [ch, *(x for x in SEPARATORS if x != ch)]
        cmd = next((c for w in wanted if (c := Catalog.find_command(pool, w)) is not None), None)
        if cmd is None:
            if ch in SEPARATORS:
                ch = "separator ('-' or '.')"
            if ch not in missing:
                missing.append(ch)
        else:
            presses.append(cmd)
    return presses, missing


@mcp.tool(title="Tune to a channel", annotations=_ACT)
async def harmony_tune_channel(
    channel: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_CHANNEL_ARG,
            description="A channel number such as 702, 5.1 or 5-1, or a favorite's name (e.g. 'ESPN').",
        ),
    ],
    hub: Hub = None,
    device: Annotated[
        str | None,
        Field(description="Press the digits on this device (name or ref). Omit to use the running activity's buttons."),
    ] = None,
    enter: Annotated[
        bool, Field(description="Press the select button (OK/Enter) after the digits, for boxes that wait for it.")
    ] = False,
) -> ActionResult:
    """Tune to a channel by pressing its digit buttons, e.g. channel='702', '5.1' or a favorite like 'ESPN'.

    The digits go out as one sequence, refused whole if the hub is busy or rate-limited. Without device they go
    through the running activity, which must route the digit buttons to the tuner (an activity that doesn't says
    so; name a device that has them). Set enter=True only if the box waits for a confirm press. This presses
    buttons like harmony_send_command does; it does not use the hub's own change-channel command.
    """
    r = await ready_hubs()
    text = channel.strip()
    favorite_name = None
    if looks_like_number(text):
        if (why := number_problem(text)) is not None:
            raise HarmonyError(why)
        number = text
    else:
        fav_hub, favorite_name, number = r.favorite(text, hub, device)
        hub = fav_hub.name  # a favorite belongs to one hub, so that's the hub the call is about
    if device is None:
        h, act = r.running(hub)
        pool, where = act.commands, f"activity {act.name}"
    else:
        h, dev = r.device(device, hub)
        pool, where = dev.commands, f"device {dev.name}"
    # ASSUMPTION H-DIGITS: the digit commands are named 0-9 (and '-' or '.'), and pressing them tunes.
    presses, missing = _digit_buttons(pool, number)
    if missing:
        have = [d.name for d in h.catalog.devices if not _digit_buttons(d.commands, number)[1]]
        instead = (
            f"Pass device= one of: {', '.join(have)}."
            if have
            else "No device on this hub has them; press the number on the remote."
        )
        if device is None:
            instead += " Or start an activity that routes the digit buttons."
        raise HarmonyError(
            f"{where} on {h.name} has no {', '.join(missing)} button, which channel {number} needs. {instead}"
        )
    pressed = [c.name for c in presses]
    if enter:
        target = presses[-1].device_id
        same = tuple(c for c in pool if c.device_id == target)
        select = next((c for w in SELECT_BUTTONS if (c := Catalog.find_command(same, w)) is not None), None)
        if select is None:
            raise HarmonyError(
                f"{where} on {h.name} has no select button ({', '.join(SELECT_BUTTONS)}) for "
                f"{h.catalog.device_name(target)}, the device that gets the digits. Call again without enter, "
                "or press the confirm button with harmony_send_command if it is on another device."
            )
        presses.append(select)
        pressed.append(select.name)
    outcome = await h.send_sequence(presses, f"tune {number}")
    named = f" ({favorite_name})" if favorite_name else ""
    done = f"tune {h.name} to channel {number}{named} by pressing {', '.join(pressed)}"
    return _result(h, outcome, done, "")


@mcp.tool(title="Get activity history", annotations=_READ)
async def harmony_get_history(
    hub: Hub = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Most events to return, 1-100.")] = 20,
    after_seq: Annotated[
        int | None, Field(ge=0, description="Only events newer than this seq (from a previous call's last_seq).")
    ] = None,
) -> HistoryPage:
    """What each hub's activity changed from and to, and when (newest first, or oldest first with after_seq).

    Answers "what was on at 9 last night?", "how long was the office TV on?" and "was it turned on from the
    remote?". Each change has from_activity and to_activity (null means everything off), previous_lasted_seconds
    (how long the previous state lasted), and source: 'this_server' for a start or power off this server sent,
    'elsewhere' for anything else (the physical remote, the Harmony app, Home Assistant): the hub can't say which.
    A recording_started event is where this server began watching a hub, not a change. Nothing is recorded while
    the server isn't running, so a time before recording_since, or between one session's last event and the next
    recording_started, is "not recorded", not "nothing happened". noticed_late means the change was found after
    a reconnect, so `at` is when it was noticed, not when it happened.
    """
    r = await ready_hubs()
    name = r.candidates(hub)[0].name if hub is not None else None  # validates hub; events are filed by name
    events, expired = r.history.page(name, after_seq, limit)
    page: HistoryPage = {
        "enabled": r.history.enabled,
        "persisted": r.history.persisting,
        "session_started": r.history.session_started,
        "recording_since": r.history.recording_since,
        "last_seq": r.history.last_seq,
        "expired": expired,
        "events": events,
    }
    if r.history.read_only and r.history.enabled:
        page["hint"] = "This is a one-shot `call`: it reads history.jsonl but records nothing."
    if not r.history.enabled:
        page["hint"] = 'History is turned off (HARMONY_HISTORY or "history" in config.json), so nothing is recorded.'
    return page


# --- resources: the static catalog, for clients that attach context ----------------------
# Resources are read by the client (e.g. @-mentioned in Claude Code), not called by the
# model. They carry the same data as the list tools; subscriptions aren't offered since
# the Claude apps don't support them yet.
def _catalog_doc(h: HubClient) -> dict[str, object]:
    cat = h.catalog
    return {
        "hub": h.name,
        "reachable": h.available,
        "channels": h.hub.channels,  # favorites for harmony_tune_channel, from config.json
        "activities": [{"ref": ref(h, a.name), "commands": [c.name for c in a.commands]} for a in cat.activities],
        "devices": [{"ref": ref(h, d.name), "commands": [c.name for c in d.commands]} for d in cat.devices],
    }


@mcp.resource("harmony://hubs", name="hubs", title="All hubs and their activities", mime_type="application/json")
def hubs_resource() -> str:
    return json.dumps([_catalog_doc(h) for h in hubs().hubs], indent=1)


@mcp.resource(
    "harmony://hubs/{hub}", name="hub", title="One hub's activities, devices and commands", mime_type="application/json"
)
def hub_resource(hub: str) -> str:
    return json.dumps(_catalog_doc(hubs().candidates(unquote(hub))[0]), indent=1)


# --- prompts: workflows a user can invoke ------------------------------------------------
@mcp.prompt(title="Fix a TV whose remote stopped working")
def fix_stuck_remote(hub: str = "") -> str:
    """Restart the running activity, the fix for a stuck Harmony Bluetooth connection (e.g. after a Shield reboot)."""
    where = f"the hub {hub!r}" if hub else "the hub that is on (ask me which if several are)"
    return (
        f"The remote isn't controlling {where}. Please:\n"
        "1. Call harmony_get_status and note the running activity on that hub.\n"
        "2. Call harmony_power_off for that hub, then harmony_get_status until its transition is null.\n"
        "3. Call harmony_start_activity with the same activity and hub.\n"
        "4. Tell me what you did. Background: the hub can stay Bluetooth-connected to a streamer (such as "
        "an NVIDIA Shield) without its buttons working, typically after the streamer reboots; restarting the "
        "activity re-establishes it."
    )


@mcp.prompt(title="Move to another remote")
def migrate_to_another_remote(target: str = "") -> str:
    """Write a checklist for rebuilding every Harmony activity on another remote (Sofabaton, Home Assistant, ...)."""
    remote_ = repr(target) if target else "another remote (ask me which one, if I haven't said)"
    return (
        f"I'm moving from my Logitech Harmony to {remote_}. Help me rebuild my setup there. Only read: don't "
        "start, stop or send anything.\n"
        "1. Call harmony_get_status for the hubs, then harmony_list_devices and harmony_list_activities.\n"
        "2. Call harmony_describe_activity for every activity on every hub (use each activity's ref).\n"
        "3. Write a rebuild checklist for that remote, per hub: first the devices to add (manufacturer and model, "
        "which is how a new remote finds them in its own database); then, per activity, which devices it turns "
        "on, the input each should be on, the devices it leaves off, and which device should get volume, "
        "transport, the d-pad, numbers and the other button groups.\n"
        "4. Where expected_state is 'unknown', or a device's power or input is unknown, say so in the checklist "
        "and ask me; never fill it in with a guess. Point out devices whose power is a toggle or set by hand, "
        "since a new remote handles those differently.\n"
        "5. Remind me that the Harmony hub gives command names, not IR codes, so each device's codes come from "
        "the new remote's database or from learning them off the original remote; and that "
        "`mcp-server-harmony export` writes the same information, plus every device's command names, to files I "
        "can keep."
    )
