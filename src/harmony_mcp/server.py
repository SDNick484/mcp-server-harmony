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

log = logging.getLogger(__name__)

_client: HubClient | None = None


def client() -> HubClient:
    assert _client is not None, "server lifespan has not started"
    return _client


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    """Open the hub connection once at startup; close it at shutdown."""
    global _client
    _client = HubClient(load_settings())
    await _client.start()
    try:
        yield
    finally:
        await _client.stop()
        _client = None


mcp = MCPServer(
    "harmony",
    instructions=(
        "Controls a Logitech Harmony Hub. Prefer activities: start_activity (e.g. 'Watch Shield') powers the "
        "right devices and switches inputs; power_off turns everything off. For buttons, send_command without a "
        "device sends through the running activity (volume goes to whatever the activity uses for volume). "
        "Call get_status first, and list_commands to see the exact names available."
    ),
    lifespan=lifespan,
)

# Annotations: explicit on every tool, since a tool without them is assumed
# destructive, non-idempotent and open-world. open_world is False everywhere:
# we only talk to one hub on the LAN. Nothing is destructive: activities and
# IR commands change what's on, not data. Starting an activity is idempotent
# (it's a state); pressing a button is not (VolumeUp twice is +2).
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


class ActivityInfo(TypedDict):
    name: str
    id: int
    running: bool


class DeviceInfo(TypedDict):
    name: str
    manufacturer: str
    model: str
    commands: int


class CommandInfo(TypedDict):
    name: str
    label: str
    group: str
    device: str


def _commands_view(catalog: Catalog, commands: tuple[Command, ...]) -> list[CommandInfo]:
    return [
        {"name": c.name, "label": c.label, "group": c.group, "device": catalog.device_name(c.device_id)}
        for c in commands
    ]


def _names(items: list[str]) -> str:
    return ", ".join(items) if items else "(none)"


@mcp.tool(title="Get hub status", annotations=_READ)
def get_status() -> Status:
    """Report whether the hub is reachable, whether the system is on, and which activity is running.

    transition is non-null while an activity is starting or powering off; other commands wait until it clears.
    """
    return client().snapshot()


@mcp.tool(title="List activities", annotations=_READ)
def list_activities() -> list[ActivityInfo]:
    """List the activities set up on the hub (e.g. 'Watch Shield'), marking the one running."""
    c = client()
    return [
        {"name": a.name, "id": a.activity_id, "running": a.activity_id == c.activity_id} for a in c.catalog.activities
    ]


@mcp.tool(title="List devices", annotations=_READ)
def list_devices() -> list[DeviceInfo]:
    """List the devices the hub controls, with how many commands each knows."""
    return [
        {"name": d.name, "manufacturer": d.manufacturer, "model": d.model, "commands": len(d.commands)}
        for d in client().catalog.devices
    ]


Target = Annotated[
    str | None,
    Field(description="An activity or device name. Omit for the running activity's buttons."),
]


@mcp.tool(title="List commands", annotations=_READ)
def list_commands(target: Target = None) -> list[CommandInfo]:
    """List the commands send_command accepts for an activity or device (default: the running activity).

    Each entry says which device actually receives it.
    """
    c = client()
    cat = c.catalog
    if target is None:
        current = c.current_activity()
        if current is None:
            raise HarmonyError("No activity is running. Name a device, or start an activity first.")
        return _commands_view(cat, current.commands)
    if (a := cat.activity(target)) is not None:
        return _commands_view(cat, a.commands)
    if (d := cat.device(target)) is not None:
        return _commands_view(cat, d.commands)
    raise HarmonyError(
        f"No activity or device named {target!r}. Activities: {_names([a.name for a in cat.activities])}. "
        f"Devices: {_names([d.name for d in cat.devices])}."
    )


@mcp.tool(title="Start an activity", annotations=_ACT_IDEMPOTENT)
async def start_activity(
    activity: Annotated[str, Field(description="Activity name from list_activities, e.g. 'Watch Shield'.")],
) -> str:
    """Start an activity: the hub powers on its devices and sets their inputs. Takes a few seconds.

    Starting the activity that is already running does nothing.
    """
    c = client()
    a = c.catalog.activity(activity)
    if a is None:
        raise HarmonyError(
            f"Unknown activity {activity!r}. Activities: {_names([x.name for x in c.catalog.activities])}"
        )
    started = await c.start_activity(a)
    return f"Started {a.name}" if started else f"{a.name} was already running"


@mcp.tool(title="Turn everything off", annotations=_ACT_IDEMPOTENT)
async def power_off() -> str:
    """Run the hub's power-off: turns off every device in the running activity."""
    return "Powered off" if await client().power_off() else "Everything was already off"


Repeat = Annotated[int, Field(ge=1, le=10, description="How many times to press it (1-10).")]


@mcp.tool(title="Send a command", annotations=_ACT)
async def send_command(
    command: Annotated[str, Field(description="Command name or label from list_commands, e.g. 'VolumeUp'.")],
    device: Annotated[
        str | None,
        Field(description="Send straight to this device. Omit to send through the running activity."),
    ] = None,
    repeat: Repeat = 1,
) -> str:
    """Press a button, e.g. command='VolumeUp' with repeat=3, or command='Pause'.

    Without device, the running activity decides which device gets it. Names come from list_commands;
    there is no way to send a raw IR code.
    """
    c = client()
    cat = c.catalog
    if device is None:
        current = c.current_activity()
        if current is None:
            raise HarmonyError("No activity is running, so name a device (see list_devices) or start an activity.")
        pool, where = current.commands, f"activity {current.name}"
    else:
        d = cat.device(device)
        if d is None:
            raise HarmonyError(f"Unknown device {device!r}. Devices: {_names([x.name for x in cat.devices])}")
        pool, where = d.commands, f"device {d.name}"
    cmd = Catalog.find_command(pool, command)
    if cmd is None:
        raise HarmonyError(f"{where} has no command {command!r}. Use list_commands to see what it accepts.")
    await c.send(cmd, repeat)
    return f"Sent {cmd.name} x{repeat} to {cat.device_name(cmd.device_id)}"
