# mcp-server-harmony

[![CI](https://github.com/SDNick484/mcp-server-harmony/actions/workflows/ci.yml/badge.svg)](https://github.com/SDNick484/mcp-server-harmony/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server for **Logitech Harmony Hubs**, one or many
(say, one per TV). It talks to each hub directly over its local websocket API, with no Home
Assistant and no Logitech cloud. It lets an MCP client such as Claude start and stop
activities, press buttons, and read what's on. It works with any remote paired to a hub (Elite,
Companion, 950), since the server talks to the hub, not the remote.

> **Status: verified against the simulator only.** The tests run the real aioharmony library
> against a wire-level fake hub, but nothing has run against a real hub yet. Every protocol
> detail that hasn't been confirmed on hardware is a named assumption (see
> [Verification status](#verification-status)), and
> [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) is the checklist for confirming them.

Not affiliated with Logitech. Built on [aioharmony](https://github.com/Harmony-Libs/aioharmony),
the library behind Home Assistant's Harmony integration.

## Design: activity-first

A Harmony is built around *activities* ("Watch Shield", "Listen to Music"): starting one
powers the right devices and sets their inputs. That's also the right level for a model:

- `start_activity("Watch Shield")` is one call; the hub handles the device choreography.
- `send_command("VolumeUp")` with no device goes **through the running activity**, so volume
  reaches the receiver and Pause reaches the Shield without the model knowing your wiring.
- `send_command(device="Onkyo AV Receiver", ...)` is there for the cases activities don't cover.

## Several hubs

One server drives all your hubs, each by its own name (the one set in the Harmony app). Tools
take an optional `hub`, and the model only needs it when a name is ambiguous:

- **Unique names just work.** `start_activity("Watch TV")` finds the one hub that has it.
- **A repeated name asks.** If both hubs have "Listen to Music", the tool returns an error
  naming both hubs, and the model asks you or retries with `hub`.
- **Hub-qualified refs.** The list tools return a `ref` such as `"Den/Listen to Music"`, which
  works anywhere a name does, so the model can copy one string.
- **Buttons follow the running activity**, when exactly one hub has one running. With both
  TVs on, `send_command("VolumeUp")` asks which.
- **`power_off`** turns off the one hub that's on, asks if several are, and takes `hub="all"`
  for every one. With `all`, each hub is reported separately, so one unreachable hub doesn't
  stop the others.

The server never guesses between hubs: each one is a different TV, and a guess turns off the
wrong room.

## Install

```sh
git clone https://github.com/SDNick484/mcp-server-harmony.git
cd mcp-server-harmony
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

Requires Python 3.11+.

## First contact

There's no pairing: a hub's local API answers anything on the LAN at **TCP 8088**.

```sh
mcp-server-harmony discover                       # SSDP: lists hubs that answer
mcp-server-harmony check --discover               # ...and adds them all
mcp-server-harmony check --host 192.168.1.60      # or one at a time, by IP
mcp-server-harmony doctor                         # each hub, layer by layer, with hints
mcp-server-harmony call start_activity activity="Den/Watch TV"     # one tool, as the model calls it
```

`check` connects, prints the hub's name, activities and devices (the exact names the model
will use), and adds the hub to `~/.config/mcp-server-harmony/config.json` with its name. Plain
`check` rechecks every configured hub. SSDP is multicast, so it doesn't cross VLANs or guest
networks; `--host` always works.

`doctor` checks TCP, provisioning, the websocket and the catalog for each hub, and warns about
activity names that more than one hub has. `doctor --dump DIR` saves a redacted copy of each
hub's config for `tests/fixtures/recorded/`.

## Use it with an MCP client

Local (stdio): Claude Code and Claude Desktop launch it themselves.

```json
{
  "mcpServers": {
    "harmony": { "command": "/path/to/mcp-server-harmony/.venv/bin/mcp-server-harmony" }
  }
}
```

For Claude Code: `claude mcp add harmony -- /path/to/.venv/bin/mcp-server-harmony`.

As a service (Streamable HTTP), to share one instance between Claude Code, Claude Desktop and
the mobile app through a Cloudflare Tunnel with Cloudflare Access in front:

```sh
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com CF_ACCESS_AUD=<aud tag> \
  mcp-server-harmony serve --http --public-host mcp.example.com     # 127.0.0.1:8713/harmony/mcp
```

Every request must carry a valid Access JWT (`Cf-Access-Jwt-Assertion`). The checks and the
flags are documented in `src/harmony_mcp/remote.py`.

## Configuration

| Setting                  | Default                               | What it does                                                    |
| ------------------------ | ------------------------------------- | --------------------------------------------------------------- |
| `HARMONY_HOSTS`          | *(from `config.json`)*                | Comma-separated hub addresses; replaces the file's list         |
| `HARMONY_CONFIG_DIR`     | `$XDG_CONFIG_HOME/mcp-server-harmony` | Where `config.json` lives                                       |
| `HARMONY_DRY_RUN`        | off                                   | Read the hubs, send nothing that changes anything (`serve --dry-run` too) |
| `HARMONY_LOG_UNREDACTED` | off                                   | Don't mask IPs and MACs in logs (`--no-redact`)                 |

`config.json` (written by `check`, safe to edit by hand):

```json
{
  "hubs": [
    { "host": "192.168.1.60", "name": "Living Room" },
    { "host": "192.168.1.61", "name": "Den" }
  ]
}
```

`name` is optional: without it, the hub's own name is used once it connects. Write your own to
call a hub something else; `check` fills in missing names but never replaces one. A hub also
answers to its address. Optionally add `"protocol": "WEBSOCKETS"` (or `"XMPP"`) to skip the
transport race. `HARMONY_HOST` (one hub) and the older `{"host": ...}` form still work.
Problems (a host with a port, duplicate hosts or names, invalid JSON) are reported by name at
startup and by `doctor`.

## Tools

| Tool              | What it does                                                                                     |
| ----------------- | ------------------------------------------------------------------------------------------------ |
| `get_status`      | Start here: every hub's name, reachable, firmware, on/off, running activity, and any `transition` |
| `list_activities` | Activities on each hub, with `ref`, marking the running ones                                    |
| `list_devices`    | Devices on each hub, with `ref`, manufacturer, model and command count                          |
| `list_commands`   | Commands for an activity or device (default: the running activity), and which device gets each  |
| `start_activity`  | Start an activity and wait until its hub finishes; `unchanged` if it's already running          |
| `power_off`       | Run a hub's power-off (or `hub="all"`); `unchanged` if it's already off                         |
| `send_command`    | Press a command 1-10 times (`repeat`), optionally held (`hold_ms`) or spaced (`delay_ms`)      |

Every tool except `get_status` takes an optional `hub`. Actions return
`{hub, outcome: done | unchanged | dry_run | error, detail, sent}`, where `sent` lists the frames
that went to the hub. The server also offers resources (`harmony://hubs`,
`harmony://hubs/{hub}`) with each hub's catalog, and a `fix_stuck_remote` prompt for the
Shield's Bluetooth keyboard getting stuck.

### A session against the simulator

Two hubs that both have "Listen to Music". (Output from `simulate`, trimmed.)

```
> get_status
  {"dry_run": false, "hubs": [
    {"hub": "Living Room", "reachable": true, "power": "off", "current_activity": null, "transition": null, ...},
    {"hub": "Den", "reachable": true, "power": "off", "current_activity": null, "transition": null, ...}]}

> start_activity {"activity": "Listen to Music"}
  ERROR: Activity 'Listen to Music' exists on Living Room, Den; pass hub to choose.

> list_activities {"hub": "Den"}
  [{"hub": "Den", "name": "Watch TV", "ref": "Den/Watch TV", "running": false},
   {"hub": "Den", "name": "Listen to Music", "ref": "Den/Listen to Music", "running": false}]

> start_activity {"activity": "Den/Listen to Music"}
  {"hub": "Den", "outcome": "done", "detail": "Start Listen to Music on Den",
   "sent": ["runactivity activityId=39000002 (Listen to Music)"]}

> start_activity {"activity": "Watch Shield"}
  {"hub": "Living Room", "outcome": "done", "detail": "Start Watch Shield on Living Room", ...}

> send_command {"command": "VolumeUp", "repeat": 3}
  ERROR: Activities are running on Living Room (Watch Shield), Den (Listen to Music); pass hub to choose.

> send_command {"command": "VolumeUp", "hub": "Living Room", "repeat": 3}
  {"hub": "Living Room", "outcome": "done", "detail": "Send VolumeUp x3 to Onkyo AV Receiver on Living Room",
   "sent": ["holdAction press+release Onkyo AV Receiver/VolumeUp", ...]}

> power_off
  ERROR: Several hubs are on (Living Room, Den); pass hub, or hub='all' for every one.

> power_off {"hub": "all"}
  {"results": [{"hub": "Living Room", "outcome": "done", "detail": "Power off Living Room", ...},
               {"hub": "Den", "outcome": "done", "detail": "Power off Den", ...}]}
```

## Safety design

- **The hub's own config is the allow-list.** Activity, device and command names are resolved
  against what the hub already knows. There's no way to send a raw IR code, an unknown device
  id, or a command from one device to another.
- **Left out on purpose:** `sync` (talks to Logitech's cloud), `change_channel`, and anything
  that edits the hub's configuration.
- **No interleaving with activity sequences.** While a hub is starting an activity or powering
  off, its commands are refused with "busy, try again", since a press mid-sequence can land on
  a device that's still warming up.
- **Rate limits and caps, per hub.** Per call: at most 10 repeats, held up to 3 s, 100-2000 ms
  apart, 15 s in total. Across calls (a model in a loop): 20 presses in a burst, then 4 a
  second; 4 activity changes in a burst, then one per 15 s. A call over the limit is refused
  whole, never half-sent.
- **Dry run.** `serve --dry-run` (or `call --dry-run`) reads the hubs and reports what it
  would send.
- **Resilient.** Each hub reconnects with backoff, and re-reads its state after a reconnect.
  An offline hub doesn't block the others, and shutdown is bounded per hub.
- **Logs are redacted** (IP addresses, MACs) and go to stderr; stdout is the MCP transport.
- Every tool carries a title and MCP annotations. Nothing is marked destructive (activities and
  button presses change what's on, not data). `send_command` is the one non-idempotent action,
  since VolumeUp twice is +2.

## Troubleshooting

| Message                                                   | Meaning                                                                                       |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `No Harmony hub configured.`                              | Run `check --host <ip>` once per hub, or set `HARMONY_HOSTS` (in the client's config)         |
| `Can't reach the Harmony hub 'Den' at ...`                | Offline, its IP changed, or TCP 8088 is blocked (a VLAN in between). Other hubs keep working |
| `... exists on Living Room, Den; pass hub to choose.`     | The name is on more than one hub: the model should ask which TV, or use a ref                |
| `Den is busy starting ...`                                | An activity is mid-sequence. Wait until `get_status` shows `transition: null` for that hub   |
| `Rate limit: refusing to ...`                             | Many presses in a short time. Wait and retry if it was deliberate                            |

`doctor` says which layer failed for each hub and what to check.

## Development

```sh
pip install -e ".[dev]"
pytest -q           # everything, no hardware: real aioharmony against fake hubs over real sockets
ruff check . && ruff format --check . && mypy
```

The simulator runs the same fake hubs as real services on 127.0.0.1 and 127.0.0.2:

```sh
mcp-server-harmony simulate --write-config /tmp/hcfg     # two hubs; --hubs 1 for one, --flaky N to drop connections
HARMONY_CONFIG_DIR=/tmp/hcfg mcp-server-harmony doctor
```

(On macOS, add the second loopback address first: `sudo ifconfig lo0 alias 127.0.0.2`.)

What the tests cover:

| File                  | Covers                                                                                           |
| --------------------- | ------------------------------------------------------------------------------------------------ |
| `test_contract.py`    | The real aioharmony against the fake hub over a socket: the wire protocol, faults, reconnects     |
| `test_catalog.py`     | Parsing a hub's config into the allow-list                                                       |
| `test_client.py`      | One hub: start, power off, presses, the busy guard                                               |
| `test_multi_hub.py`   | Several hubs: ambiguity, refs, an offline hub, partial failures                                  |
| `test_tools.py`       | The MCP contract the model sees (schemas, annotations, structured results)                      |
| `test_safety.py`      | Dry run, rate limits, caps, config validation, shutdown                                          |
| `test_tooling.py`     | `doctor`, `discover`, `call`, `simulate`, log redaction                                           |
| `test_recorded.py`    | Replays configs captured from real hubs (`doctor --dump`)                                        |
| `test_assumptions.py` | Every assumption appears here and in HARDWARE_VALIDATION.md                                     |

## Verification status

Everything is **verified against the simulator only**. These are the protocol assumptions the
code depends on (claim and source for each in `src/harmony_mcp/assumptions.py`):

| Assumption         | Confidence | Status         |
| ------------------ | ---------- | -------------- |
| `H-PORT`           | high       | simulator-only |
| `H-PROVISION`      | high       | simulator-only |
| `H-FRAMES`         | high       | simulator-only |
| `H-CONFIG-SHAPE`   | high       | simulator-only |
| `H-POWEROFF`       | high       | simulator-only |
| `H-NAME`           | high       | simulator-only |
| `H-START-SEQUENCE` | medium     | simulator-only |
| `H-PRESS-SILENT`   | medium     | simulator-only |
| `H-PRESS-ERROR`    | low        | simulator-only |
| `H-HOLD`           | low        | simulator-only |
| `H-CONFIG-PUSH`    | medium     | simulator-only |
| `H-RECONNECT`      | high       | simulator-only |
| `H-SSDP`           | medium     | simulator-only |
| `H-TIMING`         | low        | simulator-only |

"High" means aioharmony itself depends on it, and Home Assistant's Harmony integration runs on
aioharmony. `test_assumptions.py` fails if this table and `assumptions.py` disagree.

## Roadmap

- Validate on hardware ([HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md))
- Surface activity changes as MCP resource updates, once the Claude apps support subscriptions
- Publish to PyPI and the MCP registry

## License

MIT. See [LICENSE](LICENSE).
