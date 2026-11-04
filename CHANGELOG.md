# Changelog

## [Unreleased]

### Breaking Changes

- **Tool Namespacing:** All MCP tools are now prefixed with `harmony_` (`harmony_get_status`, `harmony_list_activities`, `harmony_list_devices`, `harmony_list_commands`, `harmony_start_activity`, `harmony_power_off`, `harmony_send_command`) to avoid naming collisions in MCP clients when run alongside sibling smart home servers.
- **CLI Shorthand:** The `call` CLI subcommand accepts both prefixed (`harmony_get_status`) and legacy/shorthand names (`get_status`, `status`).

### Security

- **Network Topology Masking:** Internal hub LAN IPv4 addresses in `harmony_get_status` payloads are sanitized with `mask_ip` (e.g. `192.0.2.20` -> `x.x.x.20`) to prevent leaking local network topology to LLMs over stdout.

### Documentation

- `HARDWARE_VALIDATION.md` step 7: the Office Hub results for `hold_ms` (held presses acted as one tap on the LG TV and Onkyo). `H-HOLD` is unchanged in `assumptions.py` until a configured hold action is checked.
- `HARDWARE_VALIDATION.md` steps 6, 8 and 9: results of the 2026-11-04 run on the Office and Bedroom hubs (presses, start/switch, multi-hub power off, the busy guard, config push, reconnect), with what each did not show.

### Assumptions (hardware, 2026-11-04)

- `H-FORCE-OFF` is now `hardware-contradicted`: a forced power off sent during a start from the physical remote was refused by the hub ("Activity in progress"). `power_off(force=true)` is unchanged; it still skips this server's own busy guard, but the hub can refuse it. README and CLAUDE.md say so. Whether `force` helps a hub that is really stuck is untested.
- `H-CONFIG-PUSH` is now `hardware-verified`: an activity renamed in the Harmony app showed up in `harmony_list_activities` with no restart.
- `H-RECONNECT` stays `simulator-only`, with a note: a hub unplugged and replugged came back `reachable: true` on its own, but the re-read of state after a reconnect wasn't exercised.

## [0.2.0] - 2026-10-28

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
