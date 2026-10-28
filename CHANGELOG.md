# Changelog

## [Unreleased]

### Added

- `power_off(force=true)`: sends the power off even while a hub is mid-sequence (a start stuck warming
  up), skipping the busy guard and the already-off shortcut. Still rate limited; a start it overtakes
  reports "interrupted" instead of restoring its activity. New assumption `H-FORCE-OFF`.
- `serve --http`: `/healthz` returns 503 when no hub is connected (it was always 200).
- `HubClient.ping()`, an active round trip to the hub, and `HubRegistry.healthy_count` /
  `status_summary`; `last_connected_at` and `reconnect_count` diagnostics on each hub.

### Changed

- The assumption registry uses the shared `registry.py` (from mcp-server-shared) for its record and
  queries. `doctor` no longer counts a `hardware-contradicted` assumption as "not yet confirmed"; it lists
  those on their own line (and as `contradicted_assumptions` in `--json`). None are contradicted today, so
  the output is unchanged for now. A misspelled status or a repeated id now fails at import.

### Fixed

- A crash inside a hub's connection task no longer leaves that hub offline until the server restarts:
  the task logs it, closes the half-made connection and starts over with backoff.

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
