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
