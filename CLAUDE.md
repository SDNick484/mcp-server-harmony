# CLAUDE.md

## What this is

`mcp-server-harmony`: an MCP server that controls a Logitech Harmony Hub over its local
websocket API (via `aioharmony`). Sibling of `mcp-server-onkyo` and `mcp-server-shieldtv`.

**This is a learning project.** The owner wants to understand how MCP servers are built, so
explain the reasoning behind non-obvious changes instead of only making them.

## Layout

- `src/harmony_mcp/config.py`: settings (`HARMONY_HOST` / `config.json`); nothing secret
- `src/harmony_mcp/catalog.py`: the hub's config parsed into `Activity`/`Device`/`Command`;
  this is the allow-list. Pure data, no I/O
- `src/harmony_mcp/client.py`: `HubClient`, the long-lived connection plus pushed-state cache
- `src/harmony_mcp/server.py`: MCP tools (`MCPServer` from `mcp` 2.x)
- `src/harmony_mcp/cli.py`: `serve` (default), `check [--host]`
- `tests/`: `conftest.py` (`FakeHarmonyAPI` and `SAMPLE_CONFIG`, which mirror the library's
  real shapes), `test_catalog`, `test_config`, `test_client`, `test_tools` (in-process MCP
  `Client`), `test_stdio` (installed entry point). Async tests use anyio's plugin.

## Rules for changes

- **The hub's config is the allow-list.** Every activity, device and command name is resolved
  through `Catalog`; a tool argument never becomes a device id or command string directly. No
  raw IR, no `change_channel`, no `sync` (it talks to Logitech's cloud). Widen deliberately and
  update README and tests together.
- Activity-first: `send_command` without `device` resolves through the *running activity's*
  control groups, so the model doesn't need to know which device handles volume.
- Commands are refused while `starting_id` is set (an activity's start/power-off sequence is
  running). Keep that guard: a press mid-sequence can hit a device that's still warming up.
- Raise `HarmonyError` (a `ToolError`) for anything the model or user can act on.
- Log to **stderr only**. stdout is the MCP stdio transport.
- Every tool has a `title`, explicit `ToolAnnotations`, and constrained args via
  `Annotated[..., Field(...)]`. `test_tools.py` enforces this. Nothing is destructive.

## aioharmony facts the code depends on

- `HarmonyAPI(...)` needs a running event loop at construction (it grabs it).
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

## Status

Untested on hardware. Cross-project fact from mcp-server-shieldtv: the hub is also the
Shield's Bluetooth keyboard ("Harmony Keyboard"); after a Shield reboot it can be stuck, and
the fix is power off then start the activity again.

## Commands

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-harmony check --host <ip> | (serve)
```
