"""`mcp-server-harmony doctor`: check everything between this machine and each hub, layer by layer.

First contact with real hardware fails in a handful of ways, and "it doesn't
work" doesn't say which. So each hub is checked one layer at a time, and the
first failure says what it means and what to try:

  1. config     - config.json parses; hosts are valid (Settings.problems)
  2. tcp        - something accepts a connection on the hub's port (H-PORT)
  3. provision  - the HTTP provisioning request returns activeRemoteId (H-PROVISION)
  4. connect    - aioharmony's full connect: websocket, config, name, state
  5. catalog    - activities and devices parsed; names unique across hubs

`--dump DIR` also writes each hub's raw responses (config, state digest,
discovery and provisioning info) to DIR/<hub>.json, with account details
and addresses redacted. Copy those into tests/fixtures/recorded/ and the
contract tests run against your real hubs' data (test_recorded.py).
"""

from __future__ import annotations

import asyncio
import json
import platform
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import aioharmony.hubconnector_websocket as ws_connector
import aiohttp
from aioharmony.harmonyapi import HarmonyAPI

from .assumptions import unverified
from .catalog import Catalog
from .config import HubSettings, Settings
from .logsafe import redact

# Keys whose values identify your Logitech account; dropped from dumps.
_PRIVATE_KEYS = ("email", "account", "token", "password", "user", "username")


@dataclass
class Check:
    step: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class HubReport:
    host: str
    name: str | None = None
    checks: list[Check] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    dumped_to: str | None = None

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


@dataclass
class Report:
    versions: dict[str, str]
    config_problems: list[str]
    hubs: list[HubReport]
    warnings: list[str]
    unverified_assumptions: list[str]

    @property
    def ok(self) -> bool:
        return not self.config_problems and all(h.ok for h in self.hubs) and bool(self.hubs)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("mcp-server-harmony", "mcp", "aioharmony", "aiohttp"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def scrub(value: Any) -> Any:
    """Drop account-identifying keys (recursively) before anything is written to disk."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if any(p in k.lower() for p in _PRIVATE_KEYS) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


async def _provision(host: str, port: int, timeout: float) -> dict[str, Any]:
    """The exact request aioharmony makes first (hubconnector_websocket._retrieve_hub_info)."""
    headers = {
        "Origin": "http://sl.dhg.myharmony.com",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Charset": "utf-8",
    }
    body = {"id ": 1, "cmd": "setup.account?getProvisionInfo", "params": {}}
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session,
        session.post(f"http://{host}:{port}/", json=body, headers=headers) as resp,
    ):
        data: dict[str, Any] = await resp.json(content_type=None)
        return data


async def check_hub(hub: HubSettings, settings: Settings, timeout: float, dump_dir: Path | None) -> HubReport:
    port = settings.port or ws_connector.DEFAULT_HUB_PORT
    report = HubReport(hub.host, hub.name)

    # 2. TCP
    t0 = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(hub.host, port), timeout)
        writer.close()
        report.checks.append(Check("tcp", True, f"port {port} open ({(time.perf_counter() - t0) * 1000:.0f} ms)"))
    except (OSError, TimeoutError) as exc:
        report.checks.append(
            Check(
                "tcp",
                False,
                f"nothing accepted a connection on TCP {port} ({type(exc).__name__}: {exc})",
                "Is the hub powered and on this network? Check its IP in your router (a DHCP reservation helps). "
                "A firewall, IoT VLAN or guest network between here and the hub blocks this.",
            )
        )
        return report

    # 3. Provisioning
    try:
        reply = await _provision(hub.host, port, timeout)
        remote_id = (reply.get("data") or {}).get("activeRemoteId")
        if remote_id is None:
            raise ValueError(f"reply had no data.activeRemoteId: {json.dumps(scrub(reply))[:300]}")
        report.checks.append(Check("provision", True, f"activeRemoteId {remote_id}"))
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        report.checks.append(
            Check(
                "provision",
                False,
                f"the provisioning request failed: {exc}",
                "Something answers on the port but not like a Harmony hub (ASSUMPTION H-PROVISION). Is this the "
                "hub's address? Re-run with HARMONY_LOG_UNREDACTED=1 and capture the output.",
            )
        )
        return report

    # 4. Full connect through aioharmony
    api = HarmonyAPI(ip_address=hub.host, protocol=settings.protocol or "WEBSOCKETS")
    try:
        t0 = time.perf_counter()
        connected = await asyncio.wait_for(api.connect(), timeout)
        if not connected:
            raise ConnectionError("aioharmony.connect() returned False")
        live_name = api.name if api.name != hub.host else None
        report.name = hub.name or live_name
        catalog = Catalog.from_config(api.config)
        report.activities = [a.name for a in catalog.activities]
        report.devices = [d.name for d in catalog.devices]
        current = catalog.activity_by_id(api.current_activity[0])
        report.checks.append(
            Check(
                "connect",
                True,
                f"websocket up in {(time.perf_counter() - t0) * 1000:.0f} ms; firmware {api.fw_version}; "
                f"running: {current.name if current else 'nothing (off)'}",
            )
        )
        report.checks.append(
            Check(
                "catalog",
                bool(catalog.activities),
                f"{len(report.activities)} activities, {len(report.devices)} devices",
                "" if catalog.activities else "The hub has no activities; set some up in the Harmony app.",
            )
        )
        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)
            cfg = api.hub_config
            doc = {
                "_meta": {
                    "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "versions": versions(),
                    "note": "Captured by `mcp-server-harmony doctor --dump`; account details and addresses redacted.",
                },
                "name": report.name,
                "current_activity": api.current_activity[0],
                "provision_info": scrub(cfg.info),
                "discover_info": scrub(cfg.discover_info),
                "hub_state": scrub(cfg.hub_state),
                "config": scrub(cfg.config),
            }
            slug = "".join(ch if ch.isalnum() else "-" for ch in (report.name or "hub").lower()).strip("-")
            path = dump_dir / f"{slug}.json"
            path.write_text(redact(json.dumps(doc, indent=1)) + "\n")
            report.dumped_to = str(path)
    except (Exception, TimeoutError) as exc:  # noqa: BLE001 - report anything aioharmony raises
        report.checks.append(
            Check(
                "connect",
                False,
                f"aioharmony couldn't finish connecting: {type(exc).__name__}: {exc}",
                "Provisioning worked, so the websocket or a reply it waits for failed. Re-run with "
                "HARMONY_LOG_UNREDACTED=1 -v and capture the aioharmony debug lines (HARDWARE_VALIDATION.md step 3).",
            )
        )
    finally:
        await api.close()
    return report


async def run_doctor(settings: Settings, timeout: float = 10.0, dump_dir: Path | None = None) -> Report:
    hub_reports = [await check_hub(h, settings, timeout, dump_dir) for h in settings.hubs]
    warnings: list[str] = []
    if not settings.hubs:
        warnings.append("No hubs configured: run `mcp-server-harmony check --host <ip>` or `discover`.")
    seen: dict[str, str] = {}
    for r in hub_reports:
        for a in r.activities:
            key = a.lower()
            if key in seen and seen[key] != (r.name or r.host):
                warnings.append(
                    f"Activity {a!r} exists on {seen[key]} and {r.name or r.host}: fine, but tools will ask which hub "
                    "(or take a ref like 'Den/<name>')."
                )
            seen.setdefault(key, r.name or r.host)
    return Report(
        versions=versions(),
        config_problems=list(settings.problems),
        hubs=hub_reports,
        warnings=warnings,
        unverified_assumptions=[a.id for a in unverified()],
    )


def render(report: Report) -> str:
    lines = ["Versions: " + ", ".join(f"{k} {v}" for k, v in report.versions.items())]
    if report.config_problems:
        lines.append("FAIL config:")
        lines += [f"     - {p}" for p in report.config_problems]
    else:
        lines.append("OK   config")
    for h in report.hubs:
        lines.append(f"Hub {h.name or '(name unknown)'} at {h.host}:")
        for c in h.checks:
            lines.append(f"  {'OK  ' if c.ok else 'FAIL'} {c.step:<9} {c.detail}")
            if c.hint:
                lines.append(f"       -> {c.hint}")
        if h.dumped_to:
            lines.append(f"  dumped to {h.dumped_to}")
    lines += [f"WARN {w}" for w in report.warnings]
    lines.append(
        f"{len(report.unverified_assumptions)} protocol assumptions not yet confirmed on hardware: "
        + ", ".join(report.unverified_assumptions)
        + " (see HARDWARE_VALIDATION.md)"
    )
    lines.append("All checks passed." if report.ok else "Some checks failed; see the -> hints above.")
    return redact("\n".join(lines))


def to_json(report: Report) -> str:
    doc = asdict(report)
    doc["ok"] = report.ok
    for h, src in zip(doc["hubs"], report.hubs, strict=True):
        h["ok"] = src.ok
    return redact(json.dumps(doc, indent=1))
