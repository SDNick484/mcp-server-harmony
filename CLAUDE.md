# CLAUDE.md

## What this is

`mcp-server-harmony`: an MCP server that controls one or more Logitech Harmony Hubs (one per
TV) over their local websocket API (via `aioharmony`). Sibling of `mcp-server-onkyo`,
`mcp-server-shieldtv` and `mcp-server-sofabaton`.

**This is a learning project.** The owner wants to understand how MCP servers are built, so
explain the reasoning behind non-obvious changes instead of only making them.

## Layout

- `src/harmony_mcp/config.py`: settings: the hub list (`config.json` `hubs`, or `HARMONY_HOSTS`),
  names, `protocol`, `port` (simulator only), dry-run; validated into `Settings.problems`
- `src/harmony_mcp/catalog.py`: the hub's config parsed into `Activity`/`Device`/`Command`;
  this is the allow-list. Pure data, no I/O
- `src/harmony_mcp/client.py`: `HubClient`, one hub's long-lived connection plus pushed-state cache;
  re-reads state after a reconnect; rate limits per hub
- `src/harmony_mcp/hubs.py`: `HubRegistry`, all the hubs, the rule for which one a call means,
  hub-qualified refs (`Den/Watch TV`), the first-call startup grace, bounded shutdown
- `src/harmony_mcp/server.py`: MCP tools, resources and a prompt (`MCPServer` from `mcp` 2.x)
- `src/harmony_mcp/assumptions.py`: every unverified protocol claim (id, source, confidence, status)
- `src/harmony_mcp/limits.py`, `logsafe.py`, `remote.py` (shared with siblings, keep identical)
- `src/harmony_mcp/discovery.py` (SSDP), `doctor.py`, `cli.py`: `serve` (default), `check`,
  `call`, `discover`, `doctor`, `simulate`
- `src/harmony_mcp/sim/fake_hub.py` + `fixtures/`: a wire-level fake hub (aiohttp) that the real
  aioharmony talks to; used by tests *and* `simulate`
- `tests/`: `conftest.py` (`FakeHarmonyAPI` per hub for fast unit tests, fixtures loaded from the
  package; "Listen to Music" is on both hubs on purpose), `test_contract` (real aioharmony vs the
  fake hub over sockets), `test_catalog`, `test_config`, `test_client`, `test_tools` (in-process
  MCP `Client`), `test_multi_hub`, `test_safety`, `test_tooling`, `test_recorded` (replays
  `doctor --dump` captures), `test_assumptions`, `test_cli`, `test_stdio`, `test_remote`. Async
  tests use anyio's plugin.

## Rules for changes

- **The hub's config is the allow-list.** Every activity, device and command name is resolved
  through `Catalog`; a tool argument never becomes a device id or command string directly. No
  raw IR, no `change_channel`, no `sync` (it talks to Logitech's cloud). Widen deliberately and
  update README and tests together.
- **Multiple hubs: never guess.** Every action tool takes an optional `hub`. Without it, act
  only when exactly one hub matches (the name, or the running activity); otherwise raise an
  error naming the hubs. `power_off` with several hubs on needs `hub` or `hub="all"`. Each hub
  is a different TV, so a guess turns off the wrong room.
- Hub names: `config.json` name if set, else the hub's own `friendlyName`, else its address.
  `check` saves the friendlyName but never overwrites a name already there.
- Activity-first: `send_command` without `device` resolves through the *running activity's*
  control groups, so the model doesn't need to know which device handles volume.
- Commands are refused (per hub) while `starting_id` is set (an activity's start/power-off
  sequence is running). Keep that guard: a press mid-sequence can hit a device still warming up.
- Raise `HarmonyError` (a `ToolError`) for anything the model or user can act on.
- **Don't invent protocol details.** Anything not confirmed on hardware is an `Assumption`, cited
  as `# ASSUMPTION <id>` where code depends on it, in README's table and HARDWARE_VALIDATION.md.
  `test_assumptions` enforces it.
- Log to **stderr only**, through `logsafe` (IPs and MACs redacted). stdout is the MCP stdio
  transport.
- Every tool has a `title`, explicit `ToolAnnotations`, and constrained args via
  `Annotated[..., Field(...)]`. `test_tools.py` enforces this. Nothing is destructive.

## aioharmony facts the code depends on

- `HarmonyAPI(...)` needs a running event loop at construction (it grabs it).
- `name` is the hub's `friendlyName` from its discovery info, or the IP when it has none.
- `connect()` returns False (or raises `TimeOut`) on first failure and does *not* retry; we
  do that with backoff. After the first success its websocket connector reconnects on its own
  and calls the connect/disconnect callbacks.
- Callbacks are called with one argument: connect/disconnect get the IP, the two activity
  callbacks get `(activity_id, name)`, `config_updated` gets the raw config dict.
- Config ids are strings; the API's ids are ints. Each function's `action` is a JSON string.
  Activity `-1` is the built-in PowerOff.
- `start_activity(id)` resolves only when the sequence finishes, returning `(ok, msg)`.
  `power_off()` is `start_activity(-1)`.
- `send_commands([...])` takes `SendCommandDevice(device, command, delay)` where `delay` is
  how long the button is *held*; a bare float in the list is a pause. It returns only the
  presses the hub rejected (the hub is silent on success).
- No type hints shipped (mypy override in pyproject).
- The port is one module-level constant, `hubconnector_websocket.DEFAULT_HUB_PORT`, for every hub.
  Anything that connects (registry, `check`, `doctor`) must apply `settings.port` to it.
- After an automatic reconnect it does *not* re-read the current activity or config; `HubClient`
  calls `refresh_info_from_hub()` itself (`_resync`).
- At DEBUG it logs every payload sent and received (`call -v` / `doctor -v` show the frames).

## Status

Verified against the simulator only. HARDWARE_VALIDATION.md is the checklist (`doctor` points at
its step 3, so keep the numbering). Captures from `doctor --dump` go in
`tests/fixtures/recorded/`. Cross-project fact from mcp-server-shieldtv: the hub is also the
Shield's Bluetooth keyboard ("Harmony Keyboard"); after a Shield reboot it can be stuck, and the
fix is power off then start the activity again (the `fix_stuck_remote` prompt).

## Commands

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-harmony simulate --write-config /tmp/hcfg   # fake hubs on 127.0.0.1/.2
mcp-server-harmony check --host <ip> | discover | doctor | call <tool> k=v | serve [--http]
```
