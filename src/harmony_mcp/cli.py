"""Entry point: `mcp-server-harmony [serve|check|call|discover|doctor|simulate]`.

serve      speak MCP: stdio by default (the client launches it), or --http for
           a long-lived service behind Cloudflare Access (remote.py). --dry-run
           reads the hubs but sends nothing that changes anything.
check      first contact with one hub (--host) or every configured one: list
           what it has and save it, with its own name, to config.json.
call       call one tool exactly as the model would (through an in-process MCP
           client, so arguments are validated the same way) and print the
           result: `call start_activity activity="Den/Watch TV"`; `call tools`.
discover   look for hubs with SSDP (discovery.py); `check --discover` adds them.
doctor     check each hub layer by layer and say what failed (doctor.py);
           --dump writes redacted fixtures from your real hubs.
simulate   run wire-level fake hubs (sim/fake_hub.py) on 127.0.0.1 and
           127.0.0.2, so everything above works without hardware.

There is no pairing step: a Harmony hub answers anyone on the LAN.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

from aioharmony.exceptions import HarmonyException
from aioharmony.harmonyapi import HarmonyAPI

from . import __version__, remote
from .catalog import Catalog
from .config import Protocol, config_dir, load_settings, save_hub
from .logsafe import setup_logging


def _setup_logging(args: argparse.Namespace, default: int = logging.INFO) -> None:
    verbose = getattr(args, "verbose", 0)
    level = logging.DEBUG if verbose else default
    unredacted = getattr(args, "no_redact", False) or os.environ.get("HARMONY_LOG_UNREDACTED") in ("1", "true")
    setup_logging(level, redacted=not unredacted)


# --- check ------------------------------------------------------------------------------
async def _check_one(host: str, protocol: Protocol | None) -> bool:
    api = HarmonyAPI(ip_address=host, protocol=protocol)
    try:
        if not await api.connect():
            print(f"Couldn't connect to a Harmony hub at {host} (TCP 8088). Try `doctor`.", file=sys.stderr)
            return False
    except (HarmonyException, OSError) as exc:
        print(f"Couldn't connect to {host}: {exc}. Try `doctor`.", file=sys.stderr)
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


async def _cmd_check(host: str | None, discover_first: bool = False) -> int:
    settings = load_settings()
    for p in settings.problems:
        print(f"Config: {p}", file=sys.stderr)
    if discover_first:
        from .discovery import discover

        hosts = [h.host for h in await discover()]
        if not hosts:
            print("SSDP found no hubs; use --host <ip> (see `discover --help`).", file=sys.stderr)
            return 1
    else:
        hosts = [host] if host else [h.host for h in settings.hubs]
    if not hosts:
        print("No hubs configured. Run: mcp-server-harmony check --host <ip>  (once per hub)", file=sys.stderr)
        return 2
    if settings.port is not None:
        import aioharmony.hubconnector_websocket as ws_connector

        ws_connector.DEFAULT_HUB_PORT = settings.port
    results = [await _check_one(h, settings.protocol) for h in hosts]
    return 0 if all(results) else 1


# --- discover -----------------------------------------------------------------------------
async def _cmd_discover(timeout: float, st: str, raw: bool) -> int:
    from .discovery import discover

    print(f"Searching for {st} ({timeout:.0f}s)...")
    hits = await discover(timeout=timeout, st=st)
    for hit in hits:
        print(f"  {hit.host:<16} {hit.server or ''}  {hit.location or ''}")
        if raw:
            print("    " + hit.raw.strip().replace("\n", "\n    "))
    if not hits:
        print(
            "Nothing answered. SSDP is multicast: it doesn't cross VLANs or guest networks, and on WSL2 needs "
            "mirrored networking. `check --host <ip>` always works. (Unverified on a real hub: ASSUMPTION H-SSDP.)"
        )
        return 1
    print("Add them with: mcp-server-harmony check --discover")
    return 0


# --- doctor -------------------------------------------------------------------------------
async def _cmd_doctor(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from .config import HubSettings
    from .doctor import render, run_doctor, to_json

    settings = load_settings()
    if args.host:
        settings = replace(settings, hubs=(HubSettings(args.host),))
    report = await run_doctor(settings, timeout=args.timeout, dump_dir=Path(args.dump) if args.dump else None)
    print(to_json(report) if args.json else render(report))
    return 0 if report.ok else 1


# --- simulate -----------------------------------------------------------------------------
SIM_HUBS = (("living_room", "Living Room", "127.0.0.1", 12345678), ("den", "Den", "127.0.0.2", 87654321))


async def _cmd_simulate(args: argparse.Namespace, ready: asyncio.Event | None = None) -> int:
    from .sim.fake_hub import FakeHub, load_fixture

    fakes = [
        FakeHub(load_fixture(fixture), name=name, host=host, port=args.port, remote_id=rid, step_delay=args.step_delay)
        for fixture, name, host, rid in SIM_HUBS[: args.hubs]
    ]
    try:
        for f in fakes:
            await f.start()
    except OSError as exc:
        print(
            f"Couldn't start a fake hub: {exc}. Is port {args.port} free? (On macOS add the second loopback "
            "address first: sudo ifconfig lo0 alias 127.0.0.2.)",
            file=sys.stderr,
        )
        for f in fakes:
            await f.stop()
        return 1
    cfg_dir = Path(args.write_config) if args.write_config else None
    if cfg_dir is not None:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        doc: dict[str, object] = {"protocol": "WEBSOCKETS", "hubs": [{"host": f.host} for f in fakes]}
        if args.port != 8088:
            doc["port"] = args.port
        (cfg_dir / "config.json").write_text(json.dumps(doc, indent=2) + "\n")
    print("Fake Harmony hubs (verified-against-nothing: they implement the protocol as aioharmony expects it):")
    for f in fakes:
        print(
            f"  {f.name:<12} {f.host}:{f.port}  activities: {', '.join(a['label'] for a in f.config['activity'][1:])}"
        )
    target = cfg_dir or config_dir()
    print(f"\nIn another terminal:\n  HARMONY_CONFIG_DIR={target} mcp-server-harmony doctor")
    print(f"  HARMONY_CONFIG_DIR={target} mcp-server-harmony serve      # or add it to Claude Code/Desktop")
    if cfg_dir is None:
        print("(Pass --write-config DIR to have a config.json for these written for you.)")
    print("Ctrl+C to stop.")
    sys.stdout.flush()
    if ready is not None:
        ready.set()
    try:
        while True:
            await asyncio.sleep(args.flaky or 3600)
            if args.flaky:
                print("Dropping all connections (--flaky)")
                for f in fakes:
                    await f.drop_connections()
    finally:
        for f in fakes:
            await f.stop()


async def _cmd_call(args: argparse.Namespace) -> int:
    from mcp import Client

    from .server import mcp

    tool_args: dict[str, object] = {}
    for pair in args.args:
        key, sep, raw = pair.partition("=")
        if not sep:
            print(f"arguments are key=value, got {pair!r}", file=sys.stderr)
            return 2
        try:
            tool_args[key] = json.loads(raw)  # repeat=3 -> 3, hub=null -> None
        except json.JSONDecodeError:
            tool_args[key] = raw  # activity=Watch TV -> a string
    async with Client(mcp) as c:
        if args.tool == "tools":
            for t in (await c.list_tools()).tools:
                print(f"{t.name:<16} {(t.description or '').splitlines()[0]}")
            return 0
        result = await c.call_tool(args.tool, tool_args)
    if result.is_error:
        print(" ".join(getattr(part, "text", "") for part in result.content) or "error", file=sys.stderr)
        return 1
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body["result"]
    print(json.dumps(body, indent=2))
    return 0


# --- main ---------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-server-harmony", description="MCP server for Logitech Harmony Hubs")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=0, help="debug logging (aioharmony frames too)")
    common.add_argument(
        "--no-redact", action="store_true", help="don't mask IPs and MACs in logs (HARMONY_LOG_UNREDACTED=1)"
    )
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", parents=[common], help="Run the MCP server (stdio by default, or --http)")
    serve.add_argument(
        "--dry-run",
        action="store_true",
        help="read the hubs but send nothing that changes anything (HARMONY_DRY_RUN=1)",
    )
    remote.add_http_arguments(serve, default_port=8713, default_path="/harmony/mcp")
    check = sub.add_parser(
        "check", parents=[common], help="Connect to a hub (or every configured hub), list it, and save it"
    )
    where = check.add_mutually_exclusive_group()
    where.add_argument("--host", help="A hub's IP address or hostname, to add it")
    where.add_argument("--discover", action="store_true", help="find hubs with SSDP first, then check and save each")
    call = sub.add_parser("call", parents=[common], help="Call one tool as the model would and print the result")
    call.add_argument("tool", help="tool name, or 'tools' to list them")
    call.add_argument("args", nargs="*", metavar="key=value", help="tool arguments (values are JSON if they parse)")
    call.add_argument("--dry-run", action="store_true", help="send nothing that changes anything")
    disc = sub.add_parser("discover", parents=[common], help="Look for hubs on the LAN with SSDP")
    disc.add_argument("--timeout", type=float, default=3.0)
    disc.add_argument("--st", default="urn:myharmony-com:device:harmony:1", help="search target (try ssdp:all)")
    disc.add_argument("--raw", action="store_true", help="print each reply in full")
    doc = sub.add_parser("doctor", parents=[common], help="Check config and every hub, layer by layer")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument("--dump", metavar="DIR", help="also write each hub's redacted raw data to DIR")
    doc.add_argument("--timeout", type=float, default=10.0, help="seconds per step (default 10)")
    doc.add_argument("--host", help="check this address instead of the configured hubs")
    sim = sub.add_parser("simulate", parents=[common], help="Run fake hubs locally, for testing without hardware")
    sim.add_argument("--hubs", type=int, choices=[1, 2], default=2)
    sim.add_argument("--port", type=int, default=8088, help="port for every fake hub (aioharmony uses 8088)")
    sim.add_argument("--step-delay", type=float, default=0.4, help="seconds per device while an activity starts")
    sim.add_argument("--write-config", metavar="DIR", help="write a config.json pointing at the fake hubs to DIR")
    sim.add_argument("--flaky", type=float, metavar="SECONDS", help="drop every connection every SECONDS")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd in ("check", "call", "discover", "doctor", "simulate"):
        _setup_logging(args, default=logging.WARNING)
        if getattr(args, "dry_run", False):
            os.environ["HARMONY_DRY_RUN"] = "1"
        with contextlib.suppress(KeyboardInterrupt):
            if args.cmd == "check":
                sys.exit(asyncio.run(_cmd_check(args.host, args.discover)))
            if args.cmd == "call":
                sys.exit(asyncio.run(_cmd_call(args)))
            if args.cmd == "discover":
                sys.exit(asyncio.run(_cmd_discover(args.timeout, args.st, args.raw)))
            if args.cmd == "doctor":
                sys.exit(asyncio.run(_cmd_doctor(args)))
            sys.exit(asyncio.run(_cmd_simulate(args)))
        sys.exit(130)

    _setup_logging(args)
    if getattr(args, "dry_run", False):
        os.environ["HARMONY_DRY_RUN"] = "1"  # read by load_settings in the server's lifespan
    # Imported here so the other commands don't pay for the MCP server's imports.
    from .server import mcp

    if getattr(args, "http", False):
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-harmony: {exc}\n")
    else:
        mcp.run()  # stdio: JSON-RPC over stdin/stdout
