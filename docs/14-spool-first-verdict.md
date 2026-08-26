# Spool-first control plane: validated, and it does NOT work

Status: verified live against FL Studio 2026 (26.1.5.5384, Apple Silicon) on 2026-08-26, branch `spool-first-control`.
Verdict: **the controller-script interpreter cannot write files, so a bridge-free file-based control plane is impossible.** MIDI stays for the control plane. This retires issue #8's core proposal.

## What was tested

The proposal (issue #8): drop virtual MIDI for the control plane and carry commands/responses as JSON files, polled by the controller script's `OnIdle`. Built end to end:
- controller script: `OnIdle` polls `ctl_cmd_<id>.json`, dispatches, writes `ctl_resp_<id>.json`, tombstones the command;
- server: `control_spool.py` writes commands and polls for responses (no MIDI, no bridge process).

## The three facts the live run established

Measured via a `ctl.selftest` diagnostic op called over the (working) MIDI path, so the result is independent of the spool path itself:

1. **`OnIdle` fires** — 39-40 calls observed. The polling loop runs. This unknown is retired positively.
2. **Reads work** — `os.listdir` on `Settings/Piano roll scripts/FLMCP` succeeds and sees the command files the server wrote.
3. **Writes are blocked** — every write strategy fails identically:
   `SystemError: <class '_io.FileIO'> returned NULL without setting an exception`
   Tried: direct `open(path,"w")`, `.pyscript` extension, parent `Piano roll scripts` dir, and temp-file+rename. All four blocked.

So the controller-script interpreter can *read* a command spool but cannot *write* a response spool.

## Why, and why it's not fixable from our side

The note pyscript (`MCP Write Notes.pyscript`) writes to the *same folder* fine — but it runs in FL's **piano-roll-script** interpreter, invoked from the Tools > Scripting menu. The controller script runs in FL's **MIDI-device-script** interpreter, which is sandboxed more tightly: reads allowed, writes denied. Same machine, same folder, different interpreter, different policy. Nothing we write in the controller script changes FL's sandbox.

A read-in-files / respond-over-MIDI hybrid is technically possible but pointless: if MIDI is required for responses, the port already exists, so commands ride it too. There is no MIDI-elimination win here.

## Corrected architecture (what actually makes it sleek)

MIDI stays for the control plane. The friction encountered today (virtual-port hot-plug ghosts, a separate bridge process, manual port assignment) is NOT inherent to MIDI — it came from using rtmidi virtual ports created by a separate process after FL launched. All of it is removed by:

1. **IAC driver, persistent port.** Enable a macOS IAC bus once at install. Unlike an rtmidi virtual port, it exists in CoreMIDI whether or not any host process holds it open, so FL always sees it at launch, loads the controller script, and never needs a device refresh. Kills the hot-plug fragility.
2. **Server hosts the port directly.** The MCP server opens the IAC port with rtmidi itself — no separate bridge process. `flbridge.py`/`host_probe.py` were only ever probe tools.
3. **Installer writes the MIDI assignment into `reg.xml`** (proven writable with FL closed) — no manual port dragging in FL's settings.

Net: MIDI stays, but setup is zero-config and there is no bridge process. The other sleekness wins (template `.flp`, file-only installer for scripts, `uvx` distribution) are unaffected by this verdict.

Note plane keeps using files — the pyscript (and, once it loads, the plugin) run in interpreters that *can* write, which is why that path works and this one doesn't.

## Artifacts on this branch

- `mcp-server/src/fl_mcp/control_spool.py` — the server-side spool client (kept for the record; not viable as sole transport).
- `fl-scripts/controller/device_FLMCP.py` — `OnIdle` spool poll + `ctl.selftest`/`ctl.poll` diagnostics (the diagnostics are worth keeping; they answer sandbox questions over MIDI in one call).
