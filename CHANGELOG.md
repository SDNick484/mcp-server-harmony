# Changelog

## [0.1.0] - 2026-10-28

First release on PyPI.

### Added

- MCP server for one or more Logitech Harmony Hubs over their local websocket API (`aioharmony`):
  `get_status`, `list_activities`, `list_devices`, `list_commands`, `start_activity`, `send_command` and
  `power_off`, allow-listed by each hub's own config and never guessing between hubs.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `check`, `discover`, `doctor`, `call` and
  `simulate` (wire-level fake hubs, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script, shared layout with mcp-server-webos.
- Hardware-verified on two hubs (2026-10-14): connect, status, names, power off. Everything else has run
  against the simulator only; HARDWARE_VALIDATION.md is the checklist.
