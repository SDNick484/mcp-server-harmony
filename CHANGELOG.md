# Changelog

## [Unreleased]

### Added

- `initialize` reports the package version as `serverInfo.version` (it was empty), so a bug report says which build answered.
- `harmony_get_status` with no hub configured adds a `hint` saying how to add one, instead of an empty list alone.
- `scripts/smoke_stdio.py`: launches an installed server over stdio as an MCP client does and checks the first exchange. CI's new `wheel-smoke` job runs it against the built wheel on Linux, macOS and Windows; the release workflow runs it before publishing.
- Issue templates: a bug report and a hardware report.
- `harmony_describe_activity` (read-only): the devices an activity involves, with expected power (on/off/unknown) and input, and which device gets each button group. The expected state comes from each activity's `fixit` in the hub's config (new assumption `H-FIXIT`, medium); without one it says `unknown`.
- `mcp-server-harmony export`: per hub, the raw config (account identifiers removed; `--redact` masks addresses) and a Markdown worksheet for rebuilding the setup on another remote, both readable by the owner only. A record to rebuild from, not a backup.
- `migrate_to_another_remote(target)` prompt: describes every activity and writes a rebuild checklist for the named remote.
- `harmony_set_sleep_timer(minutes, hub=None)`: turns a hub's TV system off after 1-720 minutes (0 cancels), replacing a timer already set. It runs the normal power off, so the busy guard, rate limit and dry run apply, and retries for five minutes if the hub is busy or unreachable. The hub going off first drops it; switching activities keeps it. Held in the server's memory only (it outlives the chat under `serve --http`, not over stdio).
- `harmony_get_status` gives each hub `sleep_timer` (when it fires, minutes left, or null) and `last_sleep_timer` (how the last one ended: fired, cancelled or failed, and why). Both are always present, null when empty.
- `harmony_tune_channel`: tune by pressing a channel's digits (`702`, `5.1`, `5-1`) through the running activity or a named `device`, or by a favorite's name from the hub's new `channels` map in `config.json` (shown in `harmony://hubs/{hub}`, kept by `check`). The digits go out as one sequence (`HubClient.send_sequence`): one busy check, one rate-limit charge refused whole, one dry-run report. `enter` adds the select button. New assumption `H-DIGITS`.
- `harmony_get_history(hub, limit, after_seq)`: a record of each hub's activity changes (when, from, to, how long the previous state lasted, and whether this server or something else such as the remote or app caused it), newest first or oldest first after `after_seq`, like `sofabaton_get_recent_presses`. The first state read after startup is a `recording_started` marker, not a change; a change found after a reconnect is marked `noticed_late`. Kept in memory and in a capped, owner-only `history.jsonl` in the config directory, names only, never addresses. Turn it off with `HARMONY_HISTORY=off` or `"history": false` in `config.json`. New assumptions `H-ACTIVITY-PUSH` (hardware-verified for starts from the remote) and `H-OFF-PUSH`. HARDWARE_VALIDATION step 10.

### Fixed

- `doctor --dump` could keep the Logitech account's e-mail address: a real config's `global.timeStampHash` embeds it mid-string, where neither the key list nor log redaction caught it. The scrub now drops that key and any value with an address inside it.
- `harmony_send_command` could not tell the `-` and `.` buttons apart (both normalised to nothing, so the first one won). Exact names are now matched before the normalised match.
- `mcp-server-harmony call` no longer adds `recording_started` markers to the activity history (every run appended one per hub). `call` now only reads `history.jsonl`, so `call get_history` still shows what a server recorded; `serve` records as before. A history file read while a server is mid-append, even cut inside a multi-byte character, no longer fails to load.

### Documentation

- README: the status reflects the 2026-11-04 hardware run; a Claude Desktop config using `uvx`; absolute links, so they work on PyPI.

## [0.3.0] - 2026-11-10

The v0.2.0 tag never reached PyPI (it pointed at a commit still versioned 0.1.0), so this release carries 0.2.0's changes too.

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
