"""Shared fixtures. Tests run against a fake hub: no Harmony (or network) needed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from aioharmony.const import ClientCallbackType, SendCommandDevice, SendCommandResponse

from harmony_mcp.client import HubClient
from harmony_mcp.config import Settings, load_settings
from harmony_mcp.sim.fake_hub import load_fixture

LIVING = "192.0.2.20"
DEN = "192.0.2.21"

WATCH_SHIELD = 38000001
LISTEN_MUSIC = 38000002
ONKYO = 71000001
SHIELD = 71000002
TV = 71000003
# The second hub (another TV). "Listen to Music" exists on both hubs on purpose.
DEN_WATCH = 39000001
DEN_MUSIC = 39000002
DEN_TV = 72000001


# The hub configs live in the package (sim/fixtures/*.json) so the wire-level
# fake hub, `simulate`, and these in-process fakes all use the same data.
# Shaped like HarmonyAPI.config from a real hub: string ids, a PowerOff
# pseudo-activity (-1), activities whose buttons route to different devices,
# and "Listen to Music" on both hubs on purpose.
SAMPLE_CONFIG: dict[str, Any] = load_fixture("living_room")
DEN_CONFIG: dict[str, Any] = load_fixture("den")


# Async tests use anyio's plugin (pytest.mark.anyio), not pytest-asyncio: the
# MCP SDK's in-process Client needs fixture setup and teardown in one task.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeHarmonyAPI:
    """Stands in for aioharmony.harmonyapi.HarmonyAPI.

    Mirrors the parts the client uses, *including* the shapes: the config is
    the raw nested dict with string ids, current_activity is an (id, name)
    tuple, start_activity returns (ok, msg), callbacks get one argument, and
    send_commands accepts SendCommandDevice items with bare floats as pauses
    and returns only the *failed* presses. A friendlier fake would hide bugs.
    """

    def __init__(self, config: dict[str, Any] | None = None, name: str = "Living Room") -> None:
        self.callbacks: ClientCallbackType | None = None
        self.built_with: dict[str, Any] = {}
        self.connect_results: list[bool | Exception] = []  # consumed in order; then True
        self.connect_calls = 0
        self.closed = False
        self.config: dict[str, Any] = SAMPLE_CONFIG if config is None else config
        self.name = name
        self.fw_version = "4.15.600"
        self.protocol = "WEBSOCKETS"
        self.activity_id = -1
        self.started: list[int] = []
        self.sent: list[SendCommandDevice | float] = []
        self.start_result: tuple[bool, str | None] | None = None  # override (ok, msg)
        self.start_hangs = False
        self.failing_commands: set[str] = set()

    def build(self, *, ip_address: str, protocol: str | None, callbacks: ClientCallbackType) -> FakeHarmonyAPI:
        self.built_with = {"ip_address": ip_address, "protocol": protocol}
        self.callbacks = callbacks
        return self

    @property
    def current_activity(self) -> tuple[int, str | None]:
        return self.activity_id, None

    async def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_results:
            r = self.connect_results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return True

    async def close(self) -> None:
        self.closed = True

    async def start_activity(self, activity_id: int) -> tuple[bool, str | None]:
        self.started.append(activity_id)
        if self.start_hangs:
            await asyncio.sleep(3600)
        if self.start_result is not None:
            return self.start_result
        # Like the real hub: announce "starting", then "done", then reply.
        assert self.callbacks is not None
        self.callbacks.new_activity_starting((activity_id, None))
        self.activity_id = activity_id
        self.callbacks.new_activity((activity_id, None))
        return True, "OK"

    async def send_commands(self, commands: list[Any]) -> list[SendCommandResponse]:
        self.sent.extend(commands)
        return [
            SendCommandResponse(command=c, code="417", msg="Command not found")
            for c in commands
            if isinstance(c, SendCommandDevice) and c.command in self.failing_commands
        ]

    # --- test helpers: simulate what the hub pushes ---------------------------
    def push_disconnect(self) -> None:
        assert self.callbacks is not None
        self.callbacks.disconnect(self.built_with["ip_address"])

    def push_starting(self, activity_id: int) -> None:
        assert self.callbacks is not None
        self.callbacks.new_activity_starting((activity_id, None))

    def presses(self) -> list[tuple[int, str]]:
        return [(c.device, c.command) for c in self.sent if isinstance(c, SendCommandDevice)]


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HubClient, "activity_timeout", 0.2)
    monkeypatch.setattr(HubClient, "repeat_gap", 0.0)
    monkeypatch.setattr(HubClient, "retry_delay", 0.0)


@pytest.fixture
def fakes() -> dict[str, FakeHarmonyAPI]:
    """One fake per hub address. A hub whose fake is missing never answers."""
    return {LIVING: FakeHarmonyAPI(SAMPLE_CONFIG, "Living Room"), DEN: FakeHarmonyAPI(DEN_CONFIG, "Den")}


@pytest.fixture
def fake(fakes) -> FakeHarmonyAPI:
    return fakes[LIVING]


@pytest.fixture
def factory(fakes):
    """An api_factory that hands each hub address its own fake, like HarmonyAPI(ip_address=...)."""

    def build(*, ip_address: str, protocol: str | None, callbacks: ClientCallbackType) -> FakeHarmonyAPI:
        return fakes[ip_address].build(ip_address=ip_address, protocol=protocol, callbacks=callbacks)

    return build


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """An isolated config directory (never the user's real one), with one hub."""
    monkeypatch.setenv("HARMONY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HARMONY_HOST", raising=False)
    monkeypatch.delenv("HARMONY_HOSTS", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"hubs": [{"host": LIVING}]}))
    return tmp_path


@pytest.fixture
def two_hubs(config_dir):
    """Both hubs configured; the Den's name comes from config, the Living Room's from the hub."""
    (config_dir / "config.json").write_text(json.dumps({"hubs": [{"host": LIVING}, {"host": DEN, "name": "Den"}]}))
    return config_dir


@pytest.fixture
def settings(config_dir) -> Settings:
    return load_settings()


async def wait_until(condition: Callable[[], bool], tries: int = 100) -> None:
    """Let background tasks run until condition() holds (or fail)."""
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")
