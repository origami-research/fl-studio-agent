# 34 - Per-session control broker

## Current design

FL Studio exposes one machine-wide MIDI control wire. Messages on that wire
broadcast to every running controller script, so a session request includes the
target FL process id. Each controller compares that id with its own process id;
only the matching controller executes the request and replies. A request without
an id remains a broadcast request for single-session development.

`ControlBroker` owns the one `MidiControl` instance and relays requests over a
local newline-delimited JSON socket. Socket binding decides ownership: the first
broker binds and serves, while a concurrent starter exits. This bind-wins rule
makes automatic startup race-free and prevents competing MIDI owners.

The broker handshake carries an identity derived from its module bytes and the
wire protocol version. A newer client asks a mismatched resident broker to exit,
then starts the matching broker. An unused broker exits after five minutes, so an
old environment cannot retain the machine-wide MIDI owner indefinitely.

In session mode, `BrokerControl` reads the target process id from the session's
bridge metadata, starts or connects to the broker, and sends the request with
that id. Direct `MidiControl` remains the single-session development carrier.
Both satisfy `ControlCarrier`, so callers use the same control surface.

On this Mac, FL must launch before the broker opens its ports: FL binds ports
that hot-plug after launch, while ports present at launch left the wire silent.
`fl-mcp-install` therefore runs only with FL closed and removes numbered
`FLMCP Req #N` / `FLMCP Res #N` registry duplicates before retaining the
canonical controller assignment. It writes `reg.xml.bak` first, because the
registry is user data and virtual-port instances otherwise accumulate on every
broker restart.

A session-mode FL process is never killed by the engine. FL 2026 exposes no
verified controller-script operation that reloads the current `.flp` in place,
so the file lane refuses before writing with retryable
`file_lane_requires_reload`. During a match use the Bridge for notes and the
control wire for tempo, plugin parameters, and channel levels; automation clips
and adding effects still have no live equivalent.

The note plane also requires a tick from a live Bridge process in the last 10
seconds. `fl-mcp-doctor` reports a stopped tick as not ready instead of treating its
published port as a connection. Unlock the Mac or close an FL modal dialog,
then retry; the Bridge heartbeat is written from its GUI-thread tick, not a
separate timer.

## Failure model

The broker preserves the control carrier's typed failure boundary:

- `control_plane_busy` means FL is attached but could not complete the request
  within its operation window. Retrying can be appropriate.
- `control_plane_unavailable` means the broker, wire, or target session cannot
  currently answer. The caller should restore that boundary before retrying.

An FL response is returned as a response, including an FL-level error. Transport
failures retain their category while crossing the broker socket, so a busy
controller is never reported as disconnected.

## Superseded

The earlier `CtlSpoolControl` lane used `ctl_cmd_*.json` and `ctl_resp_*.json`
files in the spool. It was refuted on 2026-09-01 because FL MIDI device scripts
cannot write files (`SystemError: FileIO returned NULL`), making a response
channel impossible. The lane from #150 was removed after the broker replacement
landed in #158.
