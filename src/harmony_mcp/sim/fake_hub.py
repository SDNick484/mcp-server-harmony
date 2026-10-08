"""A fake Harmony Hub that speaks the hub's local wire protocol.

The unit tests fake ``HarmonyAPI`` at the Python level, which never exercises
the protocol. This fake sits on the other side of the socket instead: it is
an aiohttp server on the hub's port, and the *real* aioharmony library
connects to it. So it catches mistakes in how we drive aioharmony, and in our
understanding of what the hub sends back.

What it is built from: aioharmony's source (the requests it sends, and the
replies its handlers wait for). Nothing here was recorded from a real hub,
so every behavior is tagged with the assumption it encodes (assumptions.py).
When `doctor --dump` captures a real hub, compare against this.

The protocol, as aioharmony uses it (port 8088, ASSUMPTION H-PORT):
  1. HTTP POST / {"cmd": "setup.account?getProvisionInfo"} -> {"data": {"activeRemoteId": ...}}
     (ASSUMPTION H-PROVISION)
  2. Websocket GET /?domain=...&hubId=<activeRemoteId>
  3. Frames in:  {"hubId", "timeout", "hbus": {"cmd", "id", "params"}}
     Frames out: {"cmd", "code", "id", "msg", "data"} replies, matched by id, and
                 {"type", "data"} notifications (ASSUMPTION H-FRAMES)

Faults (``FakeHub.faults``) make it misbehave on purpose: unreachable,
provisioning errors, a refused handshake, slow or malformed replies, dropped
connections, refused or never-finishing activities, rejected presses.

Run two of these with `mcp-server-harmony simulate`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from aiohttp import WSMsgType, web

log = logging.getLogger(__name__)

POWER_OFF = -1

# The commands aioharmony sends, as "<mime>?<command>" (aioharmony const.HUB_COMMANDS).
CMD_STATE_DIGEST = "vnd.logitech.connect/vnd.logitech.statedigest?get"
CMD_CONFIG = "vnd.logitech.harmony/vnd.logitech.harmony.engine?config"
CMD_DISCOVERY = "connect.discoveryinfo?get"
CMD_CURRENT = "vnd.logitech.harmony/vnd.logitech.harmony.engine?getCurrentActivity"
CMD_RUN = "harmony.activityengine?runactivity"
CMD_HOLD = "vnd.logitech.harmony/vnd.logitech.harmony.engine?holdAction"
CMD_PROVISION = "setup.account?getProvisionInfo"
# What the hub sends back while an activity runs (aioharmony handlers.py).
CMD_START_PROGRESS = "harmony.engine?startActivity"
NOTIFY_DIGEST = "connect.stateDigest?notify"
NOTIFY_FINISHED = "harmony.engine?startActivityFinished"


def load_fixture(name: str) -> dict[str, Any]:
    """A bundled hub config (sim/fixtures/<name>.json), without its _comment."""
    text = resources.files(__package__).joinpath("fixtures", f"{name}.json").read_text()
    data: dict[str, Any] = json.loads(text)
    data.pop("_comment", None)
    return data


def parse_action(raw: str) -> dict[str, Any]:
    """The action string aioharmony puts in holdAction.

    aioharmony writes it with doubled colons, '{"command":: "VolumeUp", ...}'
    (harmonyclient._send_command). That's what it sends, so that's what we
    accept; a real hub evidently tolerates it, since Home Assistant works.
    """
    parsed: dict[str, Any] = json.loads(raw.replace("::", ":"))
    return parsed


@dataclass
class Faults:
    """Switch these on (tests set fields directly; `simulate` has flags)."""

    provisioning: str = "ok"  # "ok" | "error" (HTTP 500) | "no_remote_id" | "garbage"
    handshake_status: int | None = None  # refuse the websocket upgrade with this HTTP status
    reply_delay: float = 0.0  # seconds before each websocket reply
    refuse_activity: str | None = None  # runactivity is answered with an error code and this msg
    never_finish: bool = False  # progress frames, but no final code 200
    malformed_next: int = 0  # send this many garbage text frames before the next real reply
    silent_commands: set[str] = field(default_factory=set)  # cmds that never get a reply


@dataclass
class Press:
    device_id: int
    command: str
    status: str  # "press" | "release"
    at: float


class FakeHub:
    """One fake hub. ``await start()``, point aioharmony at host:port, ``await stop()``."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        name: str,
        remote_id: int = 12345678,
        firmware: str = "4.15.600",
        host: str = "127.0.0.1",
        port: int = 0,
        step_delay: float = 0.0,
    ) -> None:
        self.config = config
        self.name = name
        self.remote_id = remote_id
        self.firmware = firmware
        self.host = host
        self.port = port
        self.step_delay = step_delay  # seconds per device in an activity's start sequence
        self.faults = Faults()
        self.current_activity = POWER_OFF
        self.config_version = 1
        self.received: list[str] = []  # every cmd received, in order (websocket and POST)
        self.presses: list[Press] = []
        self.started: list[int] = []  # activity ids whose start sequence began
        self.handshakes = 0
        self._sockets: set[web.WebSocketResponse] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._runner: web.AppRunner | None = None

    # --- lifecycle ---------------------------------------------------------------
    async def start(self) -> tuple[str, int]:
        app = web.Application()
        app.router.add_post("/", self._post)
        app.router.add_get("/", self._websocket)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        if self.port == 0:
            server = site._server
            assert server is not None and isinstance(server, asyncio.base_events.Server)
            self.port = server.sockets[0].getsockname()[1]
        log.info("Fake hub %r on %s:%s", self.name, self.host, self.port)
        return self.host, self.port

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await self.drop_connections()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def drop_connections(self) -> None:
        """Close every websocket, like a hub reboot or a Wi-Fi blip."""
        for ws in list(self._sockets):
            await ws.close()
        self._sockets.clear()

    # --- helpers for tests ---------------------------------------------------------
    def activity_ids(self) -> set[int]:
        return {int(a["id"]) for a in self.config.get("activity", [])}

    def device_commands(self) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        for d in self.config.get("device", []):
            names = out.setdefault(int(d["id"]), set())
            for group in d.get("controlGroup", []):
                for fn in group.get("function", []):
                    with contextlib.suppress(ValueError, KeyError):
                        names.add(str(json.loads(fn["action"])["command"]))
        return out

    async def push_config_change(self, config: dict[str, Any]) -> None:
        """Simulate editing the hub in the Harmony app (ASSUMPTION H-CONFIG-PUSH)."""
        self.config = config
        self.config_version += 1
        await self._broadcast({"type": NOTIFY_DIGEST, "data": self._digest()})

    # --- HTTP: provisioning --------------------------------------------------------
    async def _post(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"code": 400, "msg": "bad json"}, status=400)
        cmd = str(body.get("cmd", ""))
        self.received.append(cmd)
        if cmd != CMD_PROVISION:
            return web.json_response({"cmd": cmd, "code": 404, "msg": "unknown command"})
        fault = self.faults.provisioning
        if fault == "error":
            return web.json_response({"code": 500, "msg": "internal error"}, status=500)
        if fault == "garbage":
            return web.Response(text="<html>not json</html>", content_type="text/html")
        data: dict[str, Any] = {
            "email": "someone@example.invalid",
            "accountId": "1234567",
            "discoveryServer": "https://svcs.myharmony.com/Discovery/Discovery.svc",
            "mode": 3,
        }
        if fault != "no_remote_id":
            data["activeRemoteId"] = self.remote_id
        # ASSUMPTION H-PROVISION: the reply carries data.activeRemoteId.
        return web.json_response({"cmd": cmd, "code": 200, "msg": "OK", "data": data})

    # --- websocket -----------------------------------------------------------------
    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        if self.faults.handshake_status is not None:
            return web.Response(status=self.faults.handshake_status, text="refused by fake hub")
        if request.query.get("hubId") != str(self.remote_id):
            # A fake-only rule: we don't know what a real hub does with a wrong hubId.
            return web.Response(status=403, text="wrong hubId")
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        self.handshakes += 1
        self._sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                    hbus = frame["hbus"]
                    cmd, msgid, params = str(hbus["cmd"]), hbus.get("id"), hbus.get("params") or {}
                except (ValueError, KeyError, TypeError):
                    log.warning("Fake hub %r got an unparseable frame: %r", self.name, msg.data[:200])
                    continue
                self.received.append(cmd)
                self._spawn(self._handle(ws, cmd, msgid, params))
        finally:
            self._sockets.discard(ws)
        return ws

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        if ws.closed:
            return
        while self.faults.malformed_next > 0:
            self.faults.malformed_next -= 1
            await ws.send_str('{"cmd": "harmony.engine?startAct')  # truncated JSON
        await ws.send_str(json.dumps(frame))

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        for ws in list(self._sockets):
            await self._send(ws, frame)

    async def _reply(self, ws: web.WebSocketResponse, cmd: str, msgid: Any, data: Any = None, code: int = 200) -> None:
        if self.faults.reply_delay:
            await asyncio.sleep(self.faults.reply_delay)
        frame: dict[str, Any] = {"cmd": cmd, "code": code, "id": msgid, "msg": "OK" if code == 200 else "error"}
        if data is not None:
            frame["data"] = data
        await self._send(ws, frame)

    def _digest(self) -> dict[str, Any]:
        on = self.current_activity != POWER_OFF
        return {
            "activityId": str(self.current_activity),
            "activityStatus": 2 if on else 0,
            "configVersion": self.config_version,
            "hubSwVersion": self.firmware,
            "syncStatus": 0,
        }

    async def _handle(self, ws: web.WebSocketResponse, cmd: str, msgid: Any, params: dict[str, Any]) -> None:
        if cmd in self.faults.silent_commands:
            return
        if cmd == CMD_STATE_DIGEST:
            await self._reply(ws, cmd, msgid, self._digest())
        elif cmd == CMD_CONFIG:
            await self._reply(ws, cmd, msgid, self.config)
        elif cmd == CMD_DISCOVERY:
            # ASSUMPTION H-NAME
            await self._reply(ws, cmd, msgid, {"friendlyName": self.name, "remoteId": str(self.remote_id)})
        elif cmd == CMD_CURRENT:
            await self._reply(ws, cmd, msgid, {"result": str(self.current_activity)})
        elif cmd == CMD_RUN:
            await self._run_activity(ws, msgid, int(params.get("activityId", POWER_OFF)))
        elif cmd == CMD_HOLD:
            await self._hold(ws, msgid, params)
        else:
            await self._reply(ws, cmd, msgid, code=404)

    async def _run_activity(self, ws: web.WebSocketResponse, msgid: Any, activity_id: int) -> None:
        """ASSUMPTION H-START-SEQUENCE (order hand-built from aioharmony's handlers)."""
        if self.faults.refuse_activity is not None:
            await self._send(ws, {"cmd": CMD_RUN, "code": 400, "id": msgid, "msg": self.faults.refuse_activity})
            return
        if activity_id != POWER_OFF and activity_id not in self.activity_ids():
            await self._send(ws, {"cmd": CMD_RUN, "code": 404, "id": msgid, "msg": "unknown activity"})
            return
        self.started.append(activity_id)
        await self._reply(ws, CMD_RUN, msgid)
        status = 0 if activity_id == POWER_OFF else 1
        await self._broadcast(
            {"type": NOTIFY_DIGEST, "data": {"activityId": str(activity_id), "activityStatus": status}}
        )
        total = max(1, len(self.device_commands()))
        for done in range(1, total + 1):
            await asyncio.sleep(self.step_delay)
            await self._send(
                ws,
                {
                    "cmd": CMD_START_PROGRESS,
                    "code": 100,
                    "id": msgid,
                    "msg": "progress",
                    "data": {"done": done, "total": total},
                },
            )
        if self.faults.never_finish:
            return
        self.current_activity = activity_id
        await self._send(ws, {"cmd": CMD_START_PROGRESS, "code": 200, "id": msgid, "msg": "OK"})
        await self._broadcast({"type": NOTIFY_FINISHED, "data": {"activityId": str(activity_id), "errorCode": "200"}})

    async def _hold(self, ws: web.WebSocketResponse, msgid: Any, params: dict[str, Any]) -> None:
        try:
            action = parse_action(str(params["action"]))
            device_id, command = int(action["deviceId"]), str(action["command"])
        except (KeyError, ValueError, TypeError):
            await self._send(ws, {"cmd": CMD_HOLD, "code": "400", "id": msgid, "msg": "Bad action"})
            return
        if command not in self.device_commands().get(device_id, set()):
            # ASSUMPTION H-PRESS-ERROR: code and text are the fake's own choice.
            await self._send(ws, {"cmd": CMD_HOLD, "code": "417", "id": msgid, "msg": "Unknown command"})
            return
        loop = asyncio.get_running_loop()
        self.presses.append(Press(device_id, command, str(params.get("status", "")), loop.time()))
        # ASSUMPTION H-PRESS-SILENT: no reply on success.


async def run_fake_hubs(hubs: list[FakeHub]) -> None:
    """Start the hubs and wait until cancelled (used by `simulate`)."""
    for hub in hubs:
        await hub.start()
    try:
        await asyncio.Event().wait()
    finally:
        for hub in hubs:
            await hub.stop()
