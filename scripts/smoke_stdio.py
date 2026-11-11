"""Launch an installed server over stdio the way an MCP client does, and check the first exchange.

Usage: python scripts/smoke_stdio.py --expect-version 0.3.0 --call <status tool> -- <server command...>

This is what Claude Desktop does on first launch: spawn the command, send initialize, then
notifications/initialized and tools/list, then (here) one read-only tool call. It needs no device: run it
with an empty config directory and the status tool still answers. CI runs it against the built wheel on
Linux, macOS and Windows, so a packaging or platform problem shows up before a release instead of after.

Standard library only, so it runs with any Python and never imports the package it is testing. It fails
on anything other than JSON-RPC on stdout (the server must log to stderr only), a version mismatch, an
empty tool list, or an error from the tool call.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from typing import Any

PROTOCOL = "2025-06-18"
TIMEOUT_S = 60


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expect-version", required=True, help="serverInfo.version the server must report")
    parser.add_argument("--call", required=True, help="a read-only tool to call with no arguments")
    parser.add_argument("command", nargs="+", help="the server command, after --")
    args = parser.parse_args()

    proc = subprocess.Popen(
        args.command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",  # not the locale's: Windows would default to cp1252
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stderr: list[str] = []
    threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()  # type: ignore[arg-type]
    # A watchdog instead of select(): select() doesn't work on pipes on Windows.
    timer = threading.Timer(TIMEOUT_S, proc.kill)
    timer.start()

    def send(message: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def answer(request_id: int) -> dict[str, Any]:
        for line in proc.stdout:  # type: ignore[union-attr]
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                fail(f"non-JSON on stdout (stdout is the transport; log to stderr): {line!r}")
            if message.get("id") == request_id:
                if "error" in message:
                    fail(f"request {request_id} failed: {message['error']}")
                return message["result"]  # type: ignore[no-any-return]
        fail("the server closed stdout before answering")

    def fail(why: str) -> Any:
        proc.kill()
        timer.cancel()
        print(f"FAIL: {why}", file=sys.stderr)
        print("server stderr:\n" + "".join(stderr)[-4000:], file=sys.stderr)
        sys.exit(1)

    client = {"name": "smoke", "version": "0"}
    params = {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": client}
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    info = answer(1)["serverInfo"]
    print(f"initialize: {info}")
    if info.get("version") != args.expect_version:
        fail(f"serverInfo.version is {info.get('version')!r}, expected {args.expect_version!r}")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in answer(2)["tools"]]
    print(f"tools/list: {len(names)} tools: {', '.join(names)}")
    if args.call not in names:
        fail(f"{args.call} is not among the tools")

    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": args.call, "arguments": {}}})
    result = answer(3)
    if result.get("isError"):
        fail(f"{args.call} returned an error: {result.get('content')}")
    print(f"{args.call}: {json.dumps(result.get('structuredContent'))[:300]}")

    proc.stdin.close()  # type: ignore[union-attr]
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        fail("the server didn't exit within 15 s of stdin closing")
    timer.cancel()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
