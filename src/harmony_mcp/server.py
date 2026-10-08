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
from typing import Annotated, Literal
from urllib.parse import unquote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict

from .catalog import Catalog, Command
from .client import HarmonyError, HubClient, Outcome, Status
from .config import load_settings
from .hubs import ALL, HubRegistry, ref
from .limits import MAX_DELAY_MS, MAX_HOLD_MS, MAX_REPEAT, MIN_DELAY_MS

log = logging.getLogger(__name__)

_hubs: HubRegistry | None = None


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
    await _hubs.start()
    try:
        yield
    finally:
        await _hubs.stop()
        _hubs = None


mcp = MCPServer(
    "harmony",
    instructions=(
        "Controls Logitech Harmony Hubs, usually one per TV. Call get_status first: it lists each hub by name, "
        "whether it is on, and its running activity. Prefer activities: start_activity (e.g. 'Watch Shield') "
        "powers the right devices and switches inputs; power_off turns a hub's devices off. For buttons, "
        "send_command without a device goes through the running activity. Names can be hub-qualified "
        "('Den/Watch TV', the `ref` field of list results); pass hub only when a name exists on several hubs."
    ),
    lifespan=lifespan,
)

# Annotations: explicit on every tool, since a tool without them is assumed
# destructive, non-idempotent and open-world. open_world is False everywhere:
# we only talk to hubs on the LAN. Nothing is destructive: activities and IR
# commands change what's on, not data, and can be undone. Starting an activity
# is idempotent (it's a state); pressing a button is not (VolumeUp twice is +2).
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

Hub = Annotated[
    str | None,
    Field(description="Hub name from get_status. Omit to look across all hubs (needed only if a name repeats)."),
]


# --- result shapes (each becomes an outputSchema) ---------------------------------------
class HubsStatus(TypedDict):
    dry_run: bool  # true: writes are reported, not sent
    hubs: list[Status]


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
async def get_status() -> HubsStatus:
    """Start here. Lists every hub (one per TV) with whether it's reachable, on or off, and its running activity.

    transition is non-null while an activity is starting or powering off; that hub's commands wait until it
    clears. dry_run true means this server reports writes instead of sending them.
    """
    r = await ready_hubs()
    return {"dry_run": r.settings.dry_run, "hubs": [h.snapshot() for h in r.hubs]}


@mcp.tool(title="List activities", annotations=_READ)
async def list_activities(hub: Hub = None) -> list[ActivityInfo]:
    """List the activities on each hub (e.g. 'Watch Shield'), marking the running ones. Each has a ref usable
    anywhere an activity name is."""
    return [
        {"hub": h.name, "name": a.name, "ref": ref(h, a.name), "running": a.activity_id == h.activity_id}
        for h in (await ready_hubs()).candidates(hub)
        for a in h.catalog.activities
    ]


@mcp.tool(title="List devices", annotations=_READ)
async def list_devices(hub: Hub = None) -> list[DeviceInfo]:
    """List the devices each hub controls, with how many commands each knows. Use only when an activity
    doesn't cover what you need (send_command with device)."""
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
async def list_commands(target: Target = None, hub: Hub = None) -> CommandList:
    """List the commands send_command accepts for an activity or device (default: the running activity).

    Each entry says which device actually receives it. Activities expose the buttons they route; devices
    expose everything they know.
    """
    h, source, commands = (await ready_hubs()).commands_for(target, hub)
    return {"hub": h.name, "source": source, "commands": _commands_view(h.catalog, commands)}


# --- writes -----------------------------------------------------------------------------
@mcp.tool(title="Start an activity", annotations=_ACT_IDEMPOTENT)
async def start_activity(
    activity: Annotated[str, Field(description="Activity name or ref from list_activities, e.g. 'Den/Watch TV'.")],
    hub: Hub = None,
) -> ActionResult:
    """Start an activity: its hub powers on the devices and sets their inputs, then this returns (a few seconds).

    Starting the activity that is already running does nothing (outcome 'unchanged'). Switching activities on
    the same hub needs no power_off first.
    """
    h, a = (await ready_hubs()).activity(activity, hub)
    outcome = await h.start_activity(a)
    return _result(h, outcome, f"start {a.name} on {h.name}", f"{a.name} was already running on {h.name}")


@mcp.tool(title="Turn a TV system off", annotations=_ACT_IDEMPOTENT)
async def power_off(
    hub: Annotated[
        str | None,
        Field(description=f"Hub name, or '{ALL}' for every hub. Omit when only one hub is on."),
    ] = None,
) -> PowerOffResult:
    """Run a hub's power-off, turning off every device in its running activity.

    With no hub and several hubs on, this asks which; hub='all' turns off every one and reports each hub
    separately (one unreachable hub doesn't stop the others).
    """
    targets = (await ready_hubs()).to_power_off(hub)
    results: list[ActionResult] = []
    for h in targets:
        try:
            outcome = await h.power_off()
            results.append(_result(h, outcome, f"power off {h.name}", f"{h.name} was already off"))
        except HarmonyError as exc:
            if len(targets) == 1:
                raise
            results.append({"hub": h.name, "outcome": "error", "detail": str(exc), "sent": []})
    if results and all(r["outcome"] == "error" for r in results):
        raise HarmonyError("; ".join(f"{r['hub']}: {r['detail']}" for r in results))
    return {"results": results}


@mcp.tool(title="Send a command", annotations=_ACT)
async def send_command(
    command: Annotated[str, Field(description="Command name or label from list_commands, e.g. 'VolumeUp'.")],
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

    Without device, the running activity decides which device gets it. Names come from list_commands;
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
        raise HarmonyError(f"{where} on {h.name} has no command {command!r}. Use list_commands to see what it accepts.")
    outcome = await h.send(cmd, repeat=repeat, hold_ms=hold_ms, delay_ms=delay_ms)
    target = h.catalog.device_name(cmd.device_id)
    return _result(h, outcome, f"send {cmd.name} x{repeat} to {target} on {h.name}", "")


# --- resources: the static catalog, for clients that attach context ----------------------
# Resources are read by the client (e.g. @-mentioned in Claude Code), not called by the
# model. They carry the same data as the list tools; subscriptions aren't offered since
# the Claude apps don't support them yet.
def _catalog_doc(h: HubClient) -> dict[str, object]:
    cat = h.catalog
    return {
        "hub": h.name,
        "reachable": h.available,
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
        "1. Call get_status and note the running activity on that hub.\n"
        "2. Call power_off for that hub, then get_status until its transition is null.\n"
        "3. Call start_activity with the same activity and hub.\n"
        "4. Tell me what you did. Background: the hub can stay Bluetooth-connected to a streamer (such as "
        "an NVIDIA Shield) without its buttons working, typically after the streamer reboots; restarting the "
        "activity re-establishes it."
    )
