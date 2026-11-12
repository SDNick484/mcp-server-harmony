# Hardware validation (Harmony)

Most of this repo is verified **against the simulator only**: a wire-level fake hub
(`src/harmony_mcp/sim/fake_hub.py`) driven by the real aioharmony library. The first real-hub
run (2026-10-14) confirmed the `hardware-verified` entries in `assumptions.py`; this is the
checklist for the rest. The steps are ordered so each one depends only on the
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
`"hardware-contradicted"`, put what you saw in `note`, and keep the output. Commit both. A
misspelled status fails at import, and `doctor` then lists a contradicted assumption on its own line
rather than counting it as unconfirmed (it was checked, and the code should no longer rely on it).

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
(addresses aren't redacted, so look it over before sharing; secrets still are):

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

**Observed (2026-11-04, Office Hub with an Onkyo receiver and an LG TV, through the server from a Claude
session).** The tool names carry the `harmony_` prefix there.

- Mute twice, then four in a burst, then `repeat=10 delay_ms=100`: all returned `"outcome": "done"` with the
  presses listed in `sent`; nothing was refused or throttled.
- A harder run: five `send_command` calls in a row, each `Mute` with `repeat=10 delay_ms=100` (50 presses).
  All five returned `done` with 10 `sent` entries. Mute is a toggle and 50 is even, and the owner confirmed
  the Onkyo ended unmuted, as it started, so every press took effect and none was dropped.
- That run did **not** show whether this server's own limiter fires. The press bucket is 20 in a burst, then
  4 a second per hub (`limits.py`), and the five calls were not timed, so it is unknown whether they landed
  close enough together to exceed it. Treat the limiter as covered by `test_safety.py` only. A real check
  would time the calls and send more than 20 presses inside one second or two.
- Device-targeted and activity-routed commands both worked. A wrong command or device name returned an
  error listing the valid names. Activity, device and command refs are case-insensitive, and a bare device
  name resolves when only one hub has it.
- `send_command` with no `device` and no running activity was refused: "No activity is running on
  <hub>. Name a device, or start an activity first."

No press was ever rejected, so **H-PRESS-ERROR** is still untriggered. **H-PRESS-SILENT** stays
`simulator-only`: that claim is about what is on the wire (no reply frame on success), and no `-v` frames were
captured through the server. Only "no press was reported rejected" is shown.

## 7. Holding a button

```sh
mcp-server-harmony call send_command command=VolumeUp hold_ms=2000 hub="$HUB"
```

Watch the volume. Does it go up **one** step, or keep rising for the two seconds the way a held
button on the physical remote does? Write down which. Either is fine, and the tool description
should say which it is.

Confirms: **H-HOLD** (record the behavior in `note`).

**Observed (2026-11-04, Office Hub, manual session).** A held press did not repeat:

- `VolumeDown` with `hold_ms=200` on the Onkyo AV Receiver lowered the volume by one step.
- `Settings` pressed twice quickly, then `hold_ms=2000` on the LG TV (OLED65C7P): the held press
  behaved like one tap.
- `2` with `hold_ms=1000` on the LG TV: one press was sent; the on-screen effect was not recorded.

Harmony's detail string reports `held <n> ms`, but that only shows the timing was applied. It
doesn't show what the device received. The likely explanation: a button's hold behavior is its
own configured hold action in the Harmony setup, which falls back to the tap action when none is
set. Not verified: whether any button has a configured hold action. Until it is, `hold_ms` should
be treated as a tap with a timed press, not a sustained hold.

## 8. Power off

```sh
mcp-server-harmony call start_activity activity="<an activity on the other hub>"
mcp-server-harmony call power_off            # expected: refused, "Several hubs are on (...); pass hub, or hub='all'"
mcp-server-harmony call power_off hub="$HUB" # that TV turns off
mcp-server-harmony call power_off hub=all    # the other one (hubs that are already off aren't listed)
```

Then the forced power off. Start an activity *from the physical remote* and, while the TV is
still powering on, run:

```sh
mcp-server-harmony -v call power_off hub="$HUB" force=true
```

Everything in that activity should end up off, and `call get_status` should show `power: off`
and `transition: null`. Write down what the `-v` log shows for the interrupted start (did the
hub reply to it at all, and with which code), and whether any device was left on.

Confirms: **H-POWEROFF**, **H-FORCE-OFF** (record the reply and any device left on in `note`).

**Observed (2026-11-04, Office Hub and Bedroom Hub, through the server from a Claude session).**

- `start_activity` on the activity that is already running returns `"outcome": "unchanged"` with `sent: []`;
  nothing is sent. Switching activities on a hub needed no power off first.
- `start_activity` once returned "Bad Gateway" although the activity did start; the response was lost, not the
  start. Check `get_status` before retrying.
- With both hubs on, `power_off` with no `hub` was refused ("Several hubs are on (...); pass hub, or
  hub='all'"). `hub="Bedroom Hub"` turned off only that hub, and `hub="all"` turned off both.
- **Forced power off (H-FORCE-OFF), Bedroom Hub.** The owner started "SHIELD TV" from the physical remote.
  `get_status` showed `transition: "starting SHIELD TV"`, and `power_off(hub="Bedroom Hub", force=true)` was
  sent a few seconds into it. It failed with `Bedroom Hub refused 'power off': Activity in progress`. That text
  is the hub's own reply `msg` (aioharmony returns it as `(False, msg)` for any code other than 100 or 200;
  the string does not exist in aioharmony). `get_status` kept showing the transition, and the start then
  finished on its own (within about two minutes of the press; not timed): `power: on`, SHIELD TV running. A plain
  `power_off(hub="Bedroom Hub")` after that returned `done` with `runactivity activityId=-1 (power off)`, and
  `get_status` showed the hub off with `transition: null`. The owner's experience is that the physical remote
  ignores Off during a start too. **H-FORCE-OFF is hardware-contradicted** (one trial). Not captured: the
  reply code, and whether the hub answered the interrupted start separately, since no `-v` log was taken
  through the server. Still untested: `force` on a hub that is actually stuck. It would at least get past
  this server's own busy guard there, but nobody has seen a stuck hub yet.

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

**Observed (2026-11-04, through the server from a Claude session; items 5 and 6 not run).**

- **Step 2, config push: passed.** The Office Hub's "Office Xbox" was renamed to "Office Xbox 2" in the
  Harmony app; `list_activities` showed the new name on the first call after the owner said it was done, with
  the other six names unchanged and no restart of the server. After renaming it back, the original name
  returned the same way. The delay was not timed, only that it was under the time it took to switch to
  Claude. **H-CONFIG-PUSH is hardware-verified** (end to end; the push frame itself wasn't captured).
- **Step 3, reconnect: partly run.** The Bedroom Hub was unplugged for a couple of minutes. `get_status` said
  `reachable: true` for the first two polls after the unplug, then `reachable: false`, so a hub that just
  dropped can look reachable for a minute or two. After it was plugged back in, `reachable` went back to
  `true` by the next check, about a minute later, with no restart. **Deviation from the step:** the
  activity was started from the *Office* remote (that hub stayed up and showed "Office Xbox" correctly),
  not from the unplugged hub's own remote, and the Bedroom Hub was off the whole time. So nothing changed
  on the dropped hub while it was away, and the **re-read after reconnect was not exercised**.
  **H-RECONNECT stays `simulator-only`** with a partial note. To finish it: unplug the hub while an
  activity is running, change what it is running from its remote (or end it), plug it back in, and check
  that `get_status` shows the new state without a restart.
- **Step 4, the busy guard: refusal seen.** During a start from the physical remote a press was refused with
  "Office Hub is busy starting Office Playstation 5; send Exit once it finishes (check get_status)". Parallel
  tool calls in that harness ran one after another, so the guard can only be provoked by a start from
  elsewhere. `get_status` showed `current_activity` lagging the transition, and the owner's remote press
  chained Xbox, then PS5.

## 10. Describe and export, against the Harmony app

This checks the activity expected state (each activity's `fixit` in the hub's config), which
nothing else in this server reads.

```sh
mcp-server-harmony doctor --dump captures/
cp captures/*.json tests/fixtures/recorded/      # look them over first, as in step 3
pytest -q tests/test_recorded.py
mcp-server-harmony export --dir exports/
mcp-server-harmony call describe_activity activity="$ACT"
```

Then pick one activity and open it in the Harmony app (its devices, and each device's input), and
compare:

- Does the activity in the dump have a `fixit` object keyed by device id, with `Power` `"On"` or
  `"Off"` and an `Input` for the devices it switches? (`grep -c fixit captures/*.json`; firmware
  4.15.600 is newer than any published dump.) If there is no `fixit`, or it is shaped differently,
  **H-FIXIT is contradicted**: describe_activity and the worksheet will say `unknown` for
  everything, and the shape you see is what to record.
- In `exports/<hub>.md` and the `describe_activity` output: is every device the app shows for
  that activity listed as `on` with the input the app shows, and the others as `off`? Are the
  button groups going to the devices the app shows (for example Volume to the receiver)?
- `exports/<hub>.json` and `.md` have mode 600, and neither contains your Logitech e-mail address
  (`grep -i "@" exports/*`).

Confirms: **H-FIXIT**.

---

## Which step confirms what

| Assumption         | Step | Confidence before | Notes                                                     |
| ------------------ | ---- | ----------------- | --------------------------------------------------------- |
| `H-PORT`           | 2    | high              |                                                           |
| `H-PROVISION`      | 2    | high              |                                                           |
| `H-FRAMES`         | 3    | high              |                                                           |
| `H-CONFIG-SHAPE`   | 3    | high              | via `test_recorded`; commands resolved on hardware 2026-11-04, replay still to do |
| `H-POWEROFF`       | 8    | high              |                                                           |
| `H-NAME`           | 2    | high              |                                                           |
| `H-START-SEQUENCE` | 5    | medium            | the order of frames, from the `-v` log                    |
| `H-FORCE-OFF`      | 8    | low               | **contradicted 2026-11-04**: the hub refused it mid-start ('Activity in progress') |
| `H-PRESS-SILENT`   | 6    | medium            | no press reported rejected 2026-11-04; no `-v` frames, so still simulator-only |
| `H-PRESS-ERROR`    | none | low               | can't be triggered with commands from the hub's own config |
| `H-HOLD`           | 7    | low               | Office Hub: held presses acted as one tap (see step 7)    |
| `H-CONFIG-PUSH`    | 9    | medium            | **verified 2026-11-04** (rename showed up, no restart)    |
| `H-RECONNECT`      | 9    | high              | also confirms the re-read after reconnect; 2026-11-04 reconnect seen, re-read not exercised |
| `H-SSDP`           | 1    | medium            | no answer can be the network, not the hub                 |
| `H-TIMING`         | 5    | low               |                                                           |
| `H-FIXIT`          | 10   | medium            | shape from published dumps of older firmware; compare with the Harmony app |

## What to send back if something fails

- The full `doctor` output (redacted by default), or `doctor --json`.
- For a connect failure: `doctor-debug.log` from step 3 (unredacted, so look it over first).
- The `captures/` files from `--dump` (they have your names; secrets are removed).
- The `call` command you ran and its full output.
