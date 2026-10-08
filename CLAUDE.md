# CLAUDE.md

## What this is

`mcp-server-harmony`: an MCP server that controls one or more Logitech Harmony Hubs (one per
TV) over their local websocket API (via `aioharmony`). Sibling of `mcp-server-onkyo`,
`mcp-server-shieldtv` and `mcp-server-sofabaton`.

**This is a learning project.** The owner wants to understand how MCP servers are built, so
explain the reasoning behind non-obvious changes instead of only making them.

## Layout

- `src/harmony_mcp/config.py`: settings: the hub list (`config.json` `hubs`, or `HARMONY_HOSTS`)
  and names; nothing secret
- `src/harmony_mcp/catalog.py`: the hub's config parsed into `Activity`/`Device`/`Command`;
  this is the allow-list. Pure data, no I/O
- `src/harmony_mcp/client.py`: `HubClient`, one hub's long-lived connection plus pushed-state cache
- `src/harmony_mcp/hubs.py`: `HubRegistry`, all the hubs, and the rule for which one a call means
- `src/harmony_mcp/server.py`: MCP tools (`MCPServer` from `mcp` 2.x)
- `src/harmony_mcp/cli.py`: `serve` (default), `check [--host]`
- `tests/`: `conftest.py` (`FakeHarmonyAPI` per hub, `SAMPLE_CONFIG` and `DEN_CONFIG`, which
  mirror the library's real shapes and share an activity name on purpose), `test_catalog`,
  `test_config`, `test_client`, `test_tools` (in-process MCP `Client`, one hub),
  `test_multi_hub` (two hubs), `test_cli`, `test_stdio` (installed entry point). Async tests
  use anyio's plugin.

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
- Log to **stderr only**. stdout is the MCP stdio transport.
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

## Status

Untested on hardware. Cross-project fact from mcp-server-shieldtv: the hub is also the
Shield's Bluetooth keyboard ("Harmony Keyboard"); after a Shield reboot it can be stuck, and
the fix is power off then start the activity again.

## Commands

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-harmony check --host <ip>   # once per hub; `check` alone rechecks all
mcp-server-harmony   # serve
```
