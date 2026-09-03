# Spool-first control plane: validated, and it does NOT work

Status: verified live against FL Studio 2026 (26.1.5.5384, Apple Silicon) on 2026-08-26, branch `spool-first-control`.
Verdict: **the controller-script interpreter cannot write files, so a bridge-free file-based control plane is impossible.** MIDI stays for the control plane. This retires issue #8's core proposal.

## What was tested

The proposal (issue #8): drop virtual MIDI for the control plane and carry commands/responses as JSON files, polled by the controller script's `OnIdle`. Built end to end:
- controller script: `OnIdle` polls command files, dispatches, writes response files, then tombstones the command;
- server: `control_spool.py` writes commands and polls for responses (no MIDI, no bridge process).

## The three facts the live run established

Measured via a now-removed diagnostic op called over the working MIDI path, so the result is independent of the spool path itself:

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

## Corrected plan: implemented and validated live 2026-08-26

The MIDI-stays plan is now built and proven end to end against FL 2026:

- `mcp-server/src/fl_mcp/control_midi.py` — the server opens the virtual ports itself and speaks the SysEx control protocol directly. **No bridge process.** Validated live across three FL restarts: server opened the ports, FL bound them at launch, `hello` received, then control ops round-tripped at ~7 ms each. Reads/getters (`ping`, `getCurrentTempo`, `channelCount`, `getChannelName`, `getSongPosHint`) are reliable; event buffering (`hello`/`dirty`/`refresh`) is correctly separated from responses. `flbridge.py`/`host_probe.py` are now probe-only.

  Discovered limitation (FL-API, not transport): some *mutating* ops - `mixer.setCurrentTempo` observed - intermittently return FL's own `RuntimeError: Operation unsafe at current time` when dispatched from the OnSysEx callback mid-engine-cycle (it succeeds right after load, fails later). This is an FL scripting-thread-safety guard and would occur over any transport, including spool. Fix path (follow-up, not this branch): in the controller script, catch "unsafe at current time" in `_dispatch` and defer the op to the next `OnIdle` (which already runs), then return the deferred result. Getters and most ops are unaffected; note writes already go through the pyscript/plugin path.
- `mcp-server/src/fl_mcp/install.py` — `configure_midi()` writes the FLMCP controller assignment (input+output ports, enabled) into `reg.xml` with FL closed; `install_controller()` deploys the device script; `install_all()` does note script + controller + assignment in one call. `configure_midi()` is unit-tested for fresh insert, idempotent re-run, port normalization, and no-duplicate-keys.

Ordering requirement (documented, not a bug): the ports must exist before FL enumerates MIDI at launch — the natural user flow (start the Claude session, which spawns the server and opens the ports, then open FL). If FL is already open, restart it once. A persistent IAC bus would remove even that; it is deferred because enabling IAC programmatically is unreliable across macOS versions, and the server-hosted virtual port covers the normal flow with zero config.

Remaining manual step in the whole system: none for the control plane. The only human action left anywhere is the one-time plugin insert for the note plane's template `.flp` (crash bisect lane), which is paid once at build time.

## Artifacts on this branch

- `control_spool.py` — the server-side spool client (removed after the verdict; it was never viable as the sole transport and had no live callers). This doc is its record.
- `fl-scripts/controller/device_FLMCP.py` previously contained the `OnIdle` spool poll and sandbox diagnostics; both were removed when the broker replaced this refuted design.
