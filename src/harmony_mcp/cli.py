"""Entry point: `mcp-server-harmony` (serve) and `mcp-server-harmony check [--host IP]`.

`serve` speaks stdio by default (the client launches it). `serve --http` runs it
as a long-lived HTTP service instead, for an LXC behind Cloudflare Access; see
remote.py and the README's "Run it as a service".

There is no pairing step: a Harmony hub answers anyone on the LAN. `check` is
the first-contact tool instead. With --host it connects to that hub, prints
what it has (the names the model will use), and adds it to config.json along
with the hub's own name. Run it once per hub. Without --host it checks every
configured hub and fills in any names still missing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aioharmony.exceptions import HarmonyException
from aioharmony.harmonyapi import HarmonyAPI

from . import __version__, remote
from .catalog import Catalog
from .config import Protocol, load_settings, save_hub


def _setup_logging(level: int = logging.INFO) -> None:
    # stdout belongs to the MCP stdio transport; logs must go to stderr.
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


async def _check_one(host: str, protocol: Protocol | None) -> bool:
    api = HarmonyAPI(ip_address=host, protocol=protocol)
    try:
        if not await api.connect():
            print(f"Couldn't connect to a Harmony hub at {host} (TCP 8088).", file=sys.stderr)
            return False
    except (HarmonyException, OSError) as exc:
        print(f"Couldn't connect to {host}: {exc}", file=sys.stderr)
        return False
    try:
        # aioharmony reports the IP as the name when the hub didn't send one.
        name = api.name if api.name and api.name != host else None
        catalog = Catalog.from_config(api.config)
        current = catalog.activity_by_id(api.current_activity[0])
        print(f"{name or host} (firmware {api.fw_version}, {api.protocol}) at {host}")
        print(f"  Running: {current.name if current else 'nothing (off)'}")
        print("  Activities:")
        for a in catalog.activities:
            print(f"    {a.name}  ({len(a.commands)} commands)")
        print("  Devices:")
        for d in catalog.devices:
            print(f"    {d.name}  [{d.manufacturer} {d.model}]  ({len(d.commands)} commands)")
    finally:
        await api.close()
    path = save_hub(host, name)
    print(f"  Saved to {path}")
    return True


async def _cmd_check(host: str | None) -> int:
    settings = load_settings()
    hosts = [host] if host else [h.host for h in settings.hubs]
    if not hosts:
        print("No hubs configured. Run: mcp-server-harmony check --host <ip>  (once per hub)", file=sys.stderr)
        return 2
    results = [await _check_one(h, settings.protocol) for h in hosts]
    return 0 if all(results) else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-server-harmony", description="MCP server for Logitech Harmony Hubs")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="Run the MCP server (stdio by default, or --http)")
    remote.add_http_arguments(serve, default_port=8713, default_path="/harmony/mcp")
    check = sub.add_parser("check", help="Connect to a hub (or every configured hub), list it, and save it")
    check.add_argument("--host", help="A hub's IP address or hostname, to add it")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        _setup_logging(logging.WARNING)
        sys.exit(asyncio.run(_cmd_check(args.host)))

    _setup_logging()
    # Imported here so `check` doesn't pay for the MCP server's imports.
    from .server import mcp

    if getattr(args, "http", False):
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-harmony: {exc}\n")
    else:
        mcp.run()  # stdio: JSON-RPC over stdin/stdout
