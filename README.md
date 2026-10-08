# mcp-server-harmony

[![CI](https://github.com/SDNick484/mcp-server-harmony/actions/workflows/ci.yml/badge.svg)](https://github.com/SDNick484/mcp-server-harmony/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server for **Logitech Harmony Hubs**, one or many
(say, one per TV), talking to each hub directly over its local websocket API (no Home
Assistant, no Logitech cloud). It lets an MCP client such as Claude start and stop
activities, press buttons, and read what's on. Works with any remote paired to a hub
(Elite, Companion, 950); the server talks to the hub, not the remote.

> **Status: early / untested on hardware.** Covered by unit tests against a fake hub and
> smoke-tested over stdio, but not yet run against a real hub.

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
- **Buttons follow the running activity**, when exactly one hub has one running. With both
  TVs on, `send_command("VolumeUp")` asks which.
- **`power_off`** turns off the one hub that's on, asks if several are, and takes
  `hub="all"` for every one.

The server never guesses between hubs: each one is a different TV, and a guess turns off the
wrong room.

## Install

```
git clone https://github.com/SDNick484/mcp-server-harmony.git
cd mcp-server-harmony
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

Requires Python 3.11+.

## First contact

There's no pairing: a hub's local API answers anything on the LAN at **TCP 8088**. Run
`check` once per hub with its IP (your router's client list has them; DHCP reservations help):

```
mcp-server-harmony check --host 192.168.1.60
mcp-server-harmony check --host 192.168.1.61
```

`check` connects, prints the hub's name, activities and devices (the exact names the model
will use), and adds the hub to `~/.config/mcp-server-harmony/config.json` with its name.
Plain `mcp-server-harmony check` rechecks every configured hub.

## Use it with an MCP client

```json
{
  "mcpServers": {
    "harmony": { "command": "/path/to/mcp-server-harmony/.venv/bin/mcp-server-harmony" }
  }
}
```

For Claude Code: `claude mcp add harmony -- /path/to/.venv/bin/mcp-server-harmony`.

## Configuration

| Setting              | Default                               | What it does                                     |
| -------------------- | ------------------------------------- | ------------------------------------------------ |
| `HARMONY_HOSTS`      | *(from `config.json`)*                | Comma-separated hub addresses; replaces the file's list |
| `HARMONY_CONFIG_DIR` | `$XDG_CONFIG_HOME/mcp-server-harmony` | Where `config.json` lives                        |

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

## Tools

| Tool              | What it does                                                                      |
| ----------------- | --------------------------------------------------------------------------------- |
| `get_status`      | Every hub: name, reachable, firmware, on/off, running activity, any transition    |
| `list_activities` | Activities on each hub, marking the running ones                                  |
| `list_devices`    | Devices on each hub, with manufacturer, model and command count                   |
| `list_commands`   | Commands for an activity or device (default: the running activity), and who gets each |
| `start_activity`  | Start an activity and wait until its hub finishes; no-op if already running       |
| `power_off`       | Run a hub's power-off (or `hub="all"`); no-op if already off                      |
| `send_command`    | Press a command 1-10 times, through the running activity or straight to a device  |

Every tool except `get_status` takes an optional `hub` (see [Several hubs](#several-hubs)).
`get_status` publishes an output schema; the list tools return structured lists.

## Safety design

- **The hub's own config is the allow-list.** Activity, device and command names are resolved
  against what the hub already knows. There's no way to send a raw IR code, an unknown device
  id, or a command from one device to another.
- **Left out on purpose:** `sync` (talks to Logitech's cloud), `change_channel`, and anything
  that edits the hub's configuration.
- **No interleaving with activity sequences.** While the hub is starting an activity or
  powering off, commands are refused with "busy, try again", since a press mid-sequence can
  land on a device that's still warming up.
- Every tool carries a title and MCP annotations. Nothing is marked destructive (activities
  and button presses change what's on, not data); `send_command` is the one non-idempotent
  action, since VolumeUp twice is +2.
- Logs go to stderr; stdout belongs to the MCP transport.

## Troubleshooting

**"No Harmony hub configured."** Run `mcp-server-harmony check --host <ip>` once per hub, or
set `HARMONY_HOSTS` (an MCP client may launch the server with a different environment).

**"Can't reach the Harmony hub 'Den' at ..."** That hub is offline, its IP changed, or TCP
8088 is blocked (an IoT VLAN or guest network in between). The server keeps retrying, and
the other hubs keep working.

**"... exists on Living Room, Den; pass hub to choose."** The name is on more than one hub.
The model should ask which TV, or you can rename one of the activities in the Harmony app.

**"Den is busy starting ..."** An activity is mid-sequence. Wait for `get_status` to show
`transition: null` for that hub.

## Development

```
pip install -e ".[dev]"
pytest              # catalog, config, client, in-process MCP, and stdio tests; no hub needed
ruff check . && ruff format --check .
mypy                # strict
```

To poke at the tools interactively: `npx @modelcontextprotocol/inspector mcp-server-harmony`.

## Roadmap

- Verify on a real hub
- Discovery (the hub answers SSDP), so `check` can work without `--host`
- Publish to PyPI and the MCP registry

## License

MIT. See [LICENSE](LICENSE).
