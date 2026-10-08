# Hardware validation (Harmony)

Everything in this repo has been verified **against the simulator only**: a wire-level fake hub
(`src/harmony_mcp/sim/fake_hub.py`) driven by the real aioharmony library. This is the checklist
for the first session with real hubs. The steps are ordered so each one depends only on the
ones before it. Each step names the assumptions it confirms (ids and sources in
`src/harmony_mcp/assumptions.py`).

Plan on about 40 minutes for two hubs. `doctor` points at step 3 when a connection fails, so
keep the numbering if you edit this file.

## Before you start

- Both hubs powered, with the Harmony app able to control them (so the hubs themselves work)
- Their IP addresses (your router's client list; DHCP reservations save trouble later)
- The TVs in view, so you can see what each command does

```sh
git clone https://github.com/SDNick484/mcp-server-harmony.git && cd mcp-server-harmony
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # expect: all passed, 1 skipped (no recordings yet)
```

**Recording a result.** When a step confirms an assumption, set its `status` to
`"hardware-verified"` in `assumptions.py`. When it contradicts one, set
`"hardware-contradicted"`, put what you saw in `note`, and keep the output. Commit both.

**Tool calls** use `mcp-server-harmony call <tool> key=value ...`. It runs the tool through the
same MCP layer the model uses. `--dry-run` shows what would be sent. `call tools` lists the
tools.

---

## 1. Discovery

```sh
mcp-server-harmony discover
```

Expected: one line per hub, with its address, the SSDP `SERVER` string and the `LOCATION` URL.
`--raw` prints each full reply.

If nothing comes back, try `mcp-server-harmony discover --st ssdp:all --raw`. If the hubs answer
that but not the Harmony search target, **H-SSDP is contradicted**: record what they answered.
If nothing answers at all, multicast may be blocked between this machine and the hubs (a VLAN,
or Wi-Fi isolation). That's inconclusive rather than a contradiction. Use `--host` below.

Confirms: **H-SSDP**.

## 2. Add the hubs

```sh
mcp-server-harmony check --host <living room ip>
mcp-server-harmony check --host <den ip>
```

Expected for each: `<Name> (firmware <x>, WEBSOCKETS) at x.x.x.<n>`, then its activities and
devices, then `Saved to .../config.json`. The name should be the one set in the Harmony app.

Confirms: **H-PORT**, **H-PROVISION**, **H-NAME** (if the name matches the app's).

## 3. Each layer, per hub

```sh
mcp-server-harmony doctor
```

Expected:

```
OK   config
Hub Living Room at x.x.x.60:
  OK   tcp       port 8088 open (<n> ms)
  OK   provision activeRemoteId <n>
  OK   connect   websocket up in <n> ms; firmware <x>; running: <activity or nothing (off)>
  OK   catalog   <n> activities, <n> devices
Hub Den at x.x.x.61:
  ...
All checks passed.
```

A `WARN Activity '<name>' exists on Living Room and Den` line is fine. It means tools will
ask which hub, or take `Den/<name>`.

If `connect` fails after `provision` passed, rerun with the frames visible and keep the output
(it isn't redacted, so look it over before sharing):

```sh
mcp-server-harmony doctor -v --no-redact 2> doctor-debug.log
```

Then record what each hub returns, and replay it:

```sh
mcp-server-harmony doctor --dump captures/
cp captures/*.json tests/fixtures/recorded/
pytest -q tests/test_recorded.py      # each capture: parsed, and served back through aioharmony
```

Look the files over before committing them. They contain your activity and device names;
account details, emails and tokens are removed.

Confirms: **H-FRAMES**, **H-CONFIG-SHAPE** (if `test_recorded` passes).

## 4. Dry run, and the names the model will use

```sh
mcp-server-harmony call list_activities
mcp-server-harmony call --dry-run start_activity activity="Living Room/Watch Shield"    # yours here
```

Expected: every activity with a `ref` such as `"Den/Watch TV"`. The dry run returns
`"outcome": "dry_run"` with `sent: ["runactivity activityId=<id> (<name>)"]`, and nothing
happens on the TV.

```sh
ACT="Living Room/Watch Shield"; HUB="Living Room"; DEV="Onkyo AV Receiver"    # yours here
```

## 5. Start an activity

```sh
time mcp-server-harmony call -v start_activity activity="$ACT" 2> start.log
grep -oE '(startActivity|stateDigest)[^}]{0,80}' start.log | head -40
```

Expected: the TV and devices power on, and the call returns `"outcome": "done"` only after
they're all on. Note the time. `start.log` has every frame the hub sent (`-v` turns on
aioharmony's debug log; addresses are redacted). The `grep` should show, in order: a
`stateDigest` notify with `activityStatus` 1, some `startActivity` frames with code `100`
(progress), one with code `200`, then `startActivityFinished`.

Confirms: **H-START-SEQUENCE** (if the order matches, or record the order you see),
**H-TIMING** (if the time was under 60 s).

## 6. Presses

```sh
mcp-server-harmony call send_command command=VolumeUp repeat=3 hub="$HUB"
mcp-server-harmony call send_command command=Mute device="$DEV" hub="$HUB"
mcp-server-harmony call send_command command=Mute device="$DEV" hub="$HUB"
```

Expected: volume up three steps; mute, then unmute. Each returns `"outcome": "done"` with no
error. The hub says nothing when a press works.

**H-PRESS-ERROR** (what a rejected press looks like) can't be triggered from here: every
command comes from the hub's own config, so the hub accepts them. If you ever see a press
fail, the error text and the `-v` log lines are what to record.

Confirms: **H-PRESS-SILENT**.

## 7. Holding a button

```sh
mcp-server-harmony call send_command command=VolumeUp hold_ms=2000 hub="$HUB"
```

Watch the volume. Does it go up **one** step, or keep rising for the two seconds the way a held
button on the physical remote does? Write down which. Either is fine, and the tool description
should say which it is.

Confirms: **H-HOLD** (record the behavior in `note`).

## 8. Power off

```sh
mcp-server-harmony call start_activity activity="<an activity on the other hub>"
mcp-server-harmony call power_off            # expected: refused, "Several hubs are on (...); pass hub, or hub='all'"
mcp-server-harmony call power_off hub="$HUB" # that TV turns off
mcp-server-harmony call power_off hub=all    # the other one (hubs that are already off aren't listed)
```

Confirms: **H-POWEROFF**.

## 9. With Claude: config changes and reconnects

These need a long-running server, so use Claude Code (or Claude Desktop) with it added:

```sh
claude mcp add harmony -- "$(pwd)/.venv/bin/mcp-server-harmony"
```

1. Ask: *"What's on in the living room?"* Claude should call `get_status` first.
2. **Config push.** In the Harmony app, rename an activity (add " 2" to its name). Wait about 30
   seconds and ask Claude to list the activities. The new name should appear without restarting
   anything. Rename it back.
3. **Reconnect.** Unplug one hub for 30 seconds, plug it back in, and wait a minute. Start an
   activity *from the physical remote* while you wait. Then ask *"What's on?"* The hub should
   show `reachable: true`, and the activity you started on the remote should be shown as
   running, which proves the server re-read the state after reconnecting.
4. **The busy guard.** Start an activity *from the physical remote*, and while the TV is still
   powering on, ask Claude to turn the volume up. The press should be refused with `... is busy
   starting ...`, and Claude should retry once `get_status` shows `transition: null`. (Claude's
   own `start_activity` only returns after the sequence ends, so the guard matters for starts
   from elsewhere. It needs a long-running server, which is why it isn't a `call` step.)
5. **Ambiguity.** Ask *"Put on Listen to Music"*, using a name both hubs have. Claude should ask
   which room, not pick one.
6. **The Shield keyboard.** If the Shield's remote ever stops responding after a Shield reboot,
   try the `fix_stuck_remote` prompt.

Confirms: **H-CONFIG-PUSH** (step 2), **H-RECONNECT** (step 3).

---

## Which step confirms what

| Assumption         | Step | Confidence before | Notes                                                     |
| ------------------ | ---- | ----------------- | --------------------------------------------------------- |
| `H-PORT`           | 2    | high              |                                                           |
| `H-PROVISION`      | 2    | high              |                                                           |
| `H-FRAMES`         | 3    | high              |                                                           |
| `H-CONFIG-SHAPE`   | 3    | high              | via `test_recorded`                                       |
| `H-POWEROFF`       | 8    | high              |                                                           |
| `H-NAME`           | 2    | high              |                                                           |
| `H-START-SEQUENCE` | 5    | medium            | the order of frames, from the `-v` log                    |
| `H-PRESS-SILENT`   | 6    | medium            |                                                           |
| `H-PRESS-ERROR`    | none | low               | can't be triggered with commands from the hub's own config |
| `H-HOLD`           | 7    | low               | record which behavior you saw                             |
| `H-CONFIG-PUSH`    | 9    | medium            |                                                           |
| `H-RECONNECT`      | 9    | high              | also confirms the re-read after reconnect                 |
| `H-SSDP`           | 1    | medium            | no answer can be the network, not the hub                 |
| `H-TIMING`         | 5    | low               |                                                           |

## What to send back if something fails

- The full `doctor` output (redacted by default), or `doctor --json`.
- For a connect failure: `doctor-debug.log` from step 3 (unredacted, so look it over first).
- The `captures/` files from `--dump` (they have your names; secrets are removed).
- The `call` command you ran and its full output.
