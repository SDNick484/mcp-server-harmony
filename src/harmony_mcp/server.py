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

One server can drive several hubs (one per TV). Every tool takes an optional
``hub``; without it, names are looked up across all hubs and a name found on
more than one is an error that lists them (see hubs.py). The model never has
to pass ``hub`` while names are unique, which they usually are.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict

from .catalog import Catalog, Command
from .client import HarmonyError, HubClient, Status
from .config import load_settings
from .hubs import ALL, HubRegistry

log = logging.getLogger(__name__)

_hubs: HubRegistry | None = None


def hubs() -> HubRegistry:
    assert _hubs is not None, "server lifespan has not started"
    return _hubs


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
        "Controls Logitech Harmony Hubs (often one per TV). Prefer activities: start_activity (e.g. 'Watch Shield') "
        "powers the right devices and switches inputs; power_off turns a hub's devices off. For buttons, send_command "
        "without a device sends through the running activity. Call get_status first: it lists each hub by name. "
        "Pass hub only when a name exists on more than one hub, or to pick which TV."
    ),
    lifespan=lifespan,
)

# Annotations: explicit on every tool, since a tool without them is assumed
# destructive, non-idempotent and open-world. open_world is False everywhere:
# we only talk to hubs on the LAN. Nothing is destructive: activities and IR
# commands change what's on, not data. Starting an activity is idempotent (it's
# a state); pressing a button is not (VolumeUp twice is +2).
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

Hub = Annotated[
    str | None,
    Field(description="Hub name from get_status. Omit to look across all hubs (needed only if a name repeats)."),
]


class HubsStatus(TypedDict):
    hubs: list[Status]


class ActivityInfo(TypedDict):
    hub: str
    name: str
    running: bool


class DeviceInfo(TypedDict):
    hub: str
    name: str
    manufacturer: str
    model: str
    commands: int


class CommandInfo(TypedDict):
    name: str
    label: str
    group: str
    device: str


class CommandList(TypedDict):
    hub: str
    source: str  # "activity Watch Shield" or "device Onkyo AV Receiver"
    commands: list[CommandInfo]


def _commands_view(catalog: Catalog, commands: tuple[Command, ...]) -> list[CommandInfo]:
    return [
        {"name": c.name, "label": c.label, "group": c.group, "device": catalog.device_name(c.device_id)}
        for c in commands
    ]


@mcp.tool(title="Get hub status", annotations=_READ)
def get_status() -> HubsStatus:
    """Report each hub: its name, whether it's reachable, whether its system is on, and the running activity.

    transition is non-null while an activity is starting or powering off; that hub's commands wait until it clears.
    """
    return {"hubs": [h.snapshot() for h in hubs().hubs]}


@mcp.tool(title="List activities", annotations=_READ)
def list_activities(hub: Hub = None) -> list[ActivityInfo]:
    """List the activities on each hub (e.g. 'Watch Shield'), marking the running ones."""
    return [
        {"hub": h.name, "name": a.name, "running": a.activity_id == h.activity_id}
        for h in hubs().candidates(hub)
        for a in h.catalog.activities
    ]


@mcp.tool(title="List devices", annotations=_READ)
def list_devices(hub: Hub = None) -> list[DeviceInfo]:
    """List the devices each hub controls, with how many commands each knows."""
    return [
        {"hub": h.name, "name": d.name, "manufacturer": d.manufacturer, "model": d.model, "commands": len(d.commands)}
        for h in hubs().candidates(hub)
        for d in h.catalog.devices
    ]


Target = Annotated[
    str | None,
    Field(description="An activity or device name. Omit for the running activity's buttons."),
]


@mcp.tool(title="List commands", annotations=_READ)
def list_commands(target: Target = None, hub: Hub = None) -> CommandList:
    """List the commands send_command accepts for an activity or device (default: the running activity).

    Each entry says which device actually receives it.
    """
    h, source, commands = hubs().commands_for(target, hub)
    return {"hub": h.name, "source": source, "commands": _commands_view(h.catalog, commands)}


@mcp.tool(title="Start an activity", annotations=_ACT_IDEMPOTENT)
async def start_activity(
    activity: Annotated[str, Field(description="Activity name from list_activities, e.g. 'Watch Shield'.")],
    hub: Hub = None,
) -> str:
    """Start an activity: its hub powers on the devices and sets their inputs. Takes a few seconds.

    Starting the activity that is already running does nothing.
    """
    h, a = hubs().activity(activity, hub)
    started = await h.start_activity(a)
    return f"Started {a.name} on {h.name}" if started else f"{a.name} was already running on {h.name}"


@mcp.tool(title="Turn a TV system off", annotations=_ACT_IDEMPOTENT)
async def power_off(
    hub: Annotated[
        str | None,
        Field(description=f"Hub name, or '{ALL}' for every hub. Omit when only one hub is on."),
    ] = None,
) -> str:
    """Run a hub's power-off, turning off every device in its running activity.

    With no hub and several hubs on, this asks which; hub='all' turns off every one.
    """
    targets = hubs().to_power_off(hub)
    if not targets:
        return "Everything was already off"
    results: list[str] = []
    failed = 0
    for h in targets:
        try:
            results.append(f"{h.name}: {'powered off' if await h.power_off() else 'was already off'}")
        except HarmonyError as exc:
            if len(targets) == 1:
                raise
            failed += 1
            results.append(f"{h.name}: {exc}")
    if failed == len(targets):
        raise HarmonyError("; ".join(results))
    return "; ".join(results)


Repeat = Annotated[int, Field(ge=1, le=10, description="How many times to press it (1-10).")]


@mcp.tool(title="Send a command", annotations=_ACT)
async def send_command(
    command: Annotated[str, Field(description="Command name or label from list_commands, e.g. 'VolumeUp'.")],
    device: Annotated[
        str | None,
        Field(description="Send straight to this device. Omit to send through the running activity."),
    ] = None,
    hub: Hub = None,
    repeat: Repeat = 1,
) -> str:
    """Press a button, e.g. command='VolumeUp' with repeat=3, or command='Pause'.

    Without device, the running activity decides which device gets it. Names come from list_commands;
    there is no way to send a raw IR code.
    """
    r = hubs()
    h: HubClient
    if device is None:
        h, act = r.running(hub)
        pool, where = act.commands, f"activity {act.name}"
    else:
        h, dev = r.device(device, hub)
        pool, where = dev.commands, f"device {dev.name}"
    cmd = Catalog.find_command(pool, command)
    if cmd is None:
        raise HarmonyError(f"{where} on {h.name} has no command {command!r}. Use list_commands to see what it accepts.")
    await h.send(cmd, repeat)
    return f"Sent {cmd.name} x{repeat} to {h.catalog.device_name(cmd.device_id)} on {h.name}"
