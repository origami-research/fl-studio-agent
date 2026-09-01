# 34 - Per-session control: the pid-addressed ctl lane

## Problem (#150)

The control plane rode the MIDI wire, whose virtual port names are fixed constants (`FLMCP Req`/`FLMCP Res`).
Two FL instances cannot each get their own wire: both servers create identically-named port pairs and each FL binds one nondeterministically, so session B's tempo can land in session A's project.
Ordering rules (ports-before-launch) cannot fix an identity problem.
Observed live in the first arena matches: the wire stayed silent, every control op fell back to the file lane, and the streamed FL windows never changed.

## Design: addressing, not a new transport

The controller script already has a second control lane - `ctl_cmd_<id>.json` / `ctl_resp_<id>.json` files polled by `OnIdle` in the shared spool (`.../Piano roll scripts/FLMCP`).
Its execution surface is the full midi-scripting API (`transport`, `mixer`, `plugins`, `ui.exportWav`, composite `state.*` ops) - everything the wire could do.
Its only flaw for multi-session was that every FL instance polls the SAME folder.

So the fix is one field: each command carries the target FL's **pid**, and a controller executes only its own, leaving foreign commands untouched (no seen-marking, no tombstone).
The pid is never guessed - the server reads it per round trip from the session's `bridge_udp.json`, the one identity that survives FL's boot-time re-exec.
Request ids are namespaced by the server's pid so two fl-mcp servers sharing the folder cannot collide on response filenames.

## Selection (one fork, at construction)

`create_session()`: when `FLMCP_SPOOL_ROOT` is set (session mode), control is a `CtlSpoolControl`; the MIDI wire is **never opened** - a cross-connected wire is worse than none.
Without the env (single-session dev), the wire remains the default for its lower latency and push events.
Everything downstream is carrier-blind behind `ControlCarrier` (link.py).

## Honest limits

- The ctl lane has no push: hello is a round trip (`ctl.hello`), and dirty/refresh observation degrades in session mode until the bridge event stream carries it.
- Latency is OnIdle cadence each way - the same clock wire engine-calls already park on.
- **[ASSUMED until the next live match]** `OnIdle` fires in background (non-frontmost) FL windows; the bridge's tick does (spike-proven), the pyscript's is unverified. The first live verification match settles it; if background OnIdle stalls, control needs the bridge dispatcher instead.


## REFUTED by live probe (2026-09-01)

The single-FL probe before the first verification match killed this design:

- OnIdle fires normally in the Hardware device script (0 -> 83 calls in 4s).
- But the Hardware/MIDI **device-script sandbox cannot write files at all** -
  every `open()` path returns `SystemError: FileIO returned NULL` (confirmed
  via `ctl.selftest` over the wire: `write_ok: false`). The ctl lane needs the
  controller to write `ctl_resp_*.json`; it cannot.

Why offline tests missed it: the carrier tests faked the controller with a
normal file-writing responder, and the note lane that DOES write files is the
piano-roll pyscript (a different, writable sandbox), not the device script.
Only the device script has a persistent OnIdle loop; only it answers the wire.

**Fix direction (next workstream):** per-session control needs a transport the
device script CAN use, or a writable persistent script. Ranked:
1. Per-session **named MIDI wire** - if the device script can read a
   session discriminator (e.g. FLMCP_SPOOL_ROOT from os.environ) and derive a
   unique port-name suffix the server matches. Keeps push + low latency. Cheap
   to test: does the device script see the FL process env?
2. Bridge-dispatcher control - the native bridge writes files freely and is
   already per-session, but reaches the plugin-host ABI, not the midi-scripting
   API, so tempo/transport/params become host-call discovery work.

`CtlSpoolControl` stays in the tree (inert: session mode falls back to the
file lane exactly as before E1, no regression) until the replacement lands.


## Resolution: the single control broker (2026-09-01)

The live probe both refuted the file lane and revealed the working transport,
measured directly with two FL instances:

- The shared MIDI wire **broadcasts** - one message reaches every FL. Each
  controller runs only the request whose `pid` matches its own `os.getpid()`.
  Measured: pid-addressed round trips **10/10** to the right FL, **0** crosstalk,
  bogus-pid correctly ignored. The FL script can read its process env and pid.
- Two servers each opening the fixed-name wire **collide**: measured **0/10**.
  True per-session named ports are not viable either - FL binds the script to a
  port by fixed name via one global reg.xml, so instances cannot be given
  distinct wires.

So the machine has **exactly one wire**, owned by a **control broker** process:

- `core/carriers/control_broker.py` owns one `MidiControl` and serves a
  newline-JSON unix socket (`{pid, op, args, kwargs, timeout}` -> ControlResponse).
  Ownership is decided by the socket bind (first wins, second exits), so
  auto-start is race-free.
- `core/carriers/broker_client.py` (`BrokerControl`) is a session's carrier: it
  reads its FL pid from the session's bridge port file, auto-starts the broker
  if absent, and sends pid-addressed requests. Same `ControlCarrier` surface as
  the wire, so nothing upstream changes.
- The controller filters by pid in `_dispatch` (one line): a request for another
  FL returns None (no execution, no response).

Scales to N sessions on one machine with no configuration. No public surface
change; `ControlRequest` gains an optional `pid` (None = broadcast).
Single-session dev keeps the direct wire. `CtlSpoolControl` (the refuted file
lane) is removed.
