"""Entry point: `mcp-server-harmony` (serve) and `mcp-server-harmony check [--host IP]`.

There is no pairing step: a Harmony hub answers anyone on the LAN. `check` is
the first-contact tool instead: it connects, prints what the hub has (so you
can see the names the model will use), and saves the address on success.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aioharmony.exceptions import HarmonyException
from aioharmony.harmonyapi import HarmonyAPI

from . import __version__
from .catalog import Catalog
from .config import load_settings, save_host


def _setup_logging(level: int = logging.INFO) -> None:
    # stdout belongs to the MCP stdio transport; logs must go to stderr.
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


async def _cmd_check(host_arg: str | None) -> int:
    settings = load_settings(host_arg)
    if not settings.host:
        print("No hub address. Run: mcp-server-harmony check --host <ip>", file=sys.stderr)
        return 2
    api = HarmonyAPI(ip_address=settings.host, protocol=settings.protocol)
    try:
        if not await api.connect():
            print(f"Couldn't connect to a Harmony hub at {settings.host} (TCP 8088).", file=sys.stderr)
            return 1
    except (HarmonyException, OSError) as exc:
        print(f"Couldn't connect to {settings.host}: {exc}", file=sys.stderr)
        return 1
    try:
        catalog = Catalog.from_config(api.config)
        current = catalog.activity_by_id(api.current_activity[0])
        print(f"{api.name} (firmware {api.fw_version}, {api.protocol}) at {settings.host}")
        print(f"Running: {current.name if current else 'nothing (off)'}")
        print("Activities:")
        for a in catalog.activities:
            print(f"  {a.name}  ({len(a.commands)} commands)")
        print("Devices:")
        for d in catalog.devices:
            print(f"  {d.name}  [{d.manufacturer} {d.model}]  ({len(d.commands)} commands)")
    finally:
        await api.close()
    if host_arg:
        print(f"Saved host to {save_host(host_arg)}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-server-harmony", description="MCP server for Logitech Harmony Hub")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Run the MCP server over stdio (default)")
    check = sub.add_parser("check", help="Connect to the hub, list what it has, and save its address")
    check.add_argument("--host", help="The hub's IP address or hostname")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        _setup_logging(logging.WARNING)
        sys.exit(asyncio.run(_cmd_check(args.host)))

    _setup_logging()
    # Imported here so `check` doesn't pay for the MCP server's imports.
    from .server import mcp

    mcp.run()  # stdio transport by default
