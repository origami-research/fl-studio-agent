# 03 - Wire Protocols

Two protocols, one per in-FL endpoint. Both are versioned, request/response,
correlation-ID'd, and designed to be debuggable by a human with a text editor and a
MIDI monitor.

---

## Protocol A: SysEx control protocol (MCP server ⇄ controller script)

### Transport

Two virtual MIDI loopback ports:

- **`FLMCP Req`** - server → FL. Attached to the controller script in FL's MIDI
  *input* settings (assign a port number, enable the script as controller type).
- **`FLMCP Res`** - FL → server. Assigned in FL's MIDI *output* settings; the script
  sends via `device.midiOutSysex` (exact function name validated in Spike 3).

Port creation: on macOS the server creates both as CoreMIDI virtual ports at startup
(python-rtmidi supports this) [VERIFIED - Flapi does exactly this]. On Windows, virtual
ports cannot be created by an unprivileged normal API - the installer sets up
**loopMIDI** ports, or on Win11+ uses **Windows MIDI Services** command-line loopback
endpoints [REPORTED - BeatSage]. Install tooling handles this; the protocol is
identical either way.

### The 7-bit problem

Every byte between SysEx start `F0` and end `F7` **must be ≤ 0x7F** - MIDI data bytes
are 7-bit [VERIFIED - MIDI spec]. Arbitrary payloads must be encoded. v1 uses
**base64** of a UTF-8 JSON payload: ~33% overhead but human-decodable and
implementable identically in embedded Python 3.9 (`base64`, `json` - stdlib
availability inside FL is [ASSUMED], validated in Spike 3; fallback is a hand-rolled
7-in-8 packer, ~14% overhead).

### Frame format

```
F0 7D 4D 43 <ver> <type> <payload: base64 bytes as 7-bit ASCII> F7
│  │  │  │   │     │
│  │  │  │   │     └─ 0x01 request | 0x02 response | 0x03 event | 0x04 chunk
│  │  │  │   └─ protocol version, 0x01
│  │  └──┴─ "MC" magic, distinguishes our traffic from anything else on the port
└──└─ SysEx start + manufacturer ID 0x7D (educational/prototyping, non-commercial
      reserved ID - correct for v1; register or switch to a proper extended ID
      before any commercial release)
```

### Payload schema (JSON)

Request:
```json
{ "id": 421, "op": "mixer.setTrackVolume", "args": [4, 0.85], "kwargs": {} }
```

Response:
```json
{ "id": 421, "ok": true, "ret": null }
{ "id": 421, "ok": false, "err": "TypeError: ...", "trace": "..." }
```

- `id` - monotonically increasing per session; correlates request/response.
  Responses may arrive out of order (some ops complete on a later `OnIdle`).
- `op` - namespaced as `<module>.<function>` mirroring the FL Python API 1:1
  (`transport.start`, `plugins.setParamValue`, …) plus a small set of composite ops
  (`state.snapshot`, `ping`, `hello`). Keeping the wire ops 1:1 with the documented API
  means the protocol needs no redesign when we expose more surface - only the MCP
  tool layer (06) curates.
- **Chunking**: SysEx messages should stay small (drivers vary; we cap frames at 512
  bytes). Larger payloads are split into `0x04` chunk frames:
  `{"id":421,"seq":0,"of":3,"data":"..."}`, reassembled by recipient. The control
  plane rarely needs this (note data goes over the spool).

### Handshake & liveness

- On script `OnInit` → event frame `{"op":"hello","script_ver":...,"fl_ver":...,"ppq":...}`.
- Server pings every 5 s (`{"op":"ping"}`); two missed pongs ⇒ control plane down.
- Version mismatch (`ver` byte or `hello` contents) ⇒ server refuses with a clear
  upgrade message rather than limping.

---

## Protocol B: Spool protocol (MCP server ⇄ Bridge plugin)

### Directory layout

```
<spool root>/<session-id>/
├── commands/     # server writes, plugin consumes
│   ├── 000001.json
│   └── 000002.json
├── responses/    # plugin writes, server consumes
│   └── 000001.json
└── session.json  # {proto_ver, server_pid, started_at, heartbeat_at}
```

- **Spool root**: a fixed per-user location - macOS
  `~/Library/Application Support/FLMCP/spool/`, Windows `%LOCALAPPDATA%\FLMCP\spool\`.
  The plugin also checks `FHD_GetPath` FL data dirs for a `flmcp_spool_override` file
  redirecting elsewhere (for tests). Plugin discovers the newest `session.json` with a
  fresh heartbeat.
- **Atomicity**: writers create `*.tmp` then `rename()` - readers never see partial
  JSON. [Standard technique; rename is atomic on same filesystem on both platforms.]
- **Ordering**: zero-padded sequence filenames; plugin processes in lexical order, at
  most N (default 4) per Idle tick.
- **Consumption**: plugin renames processed command to `commands/done/` (kept for
  debugging, GC'd by server) after writing the response. A command with neither
  response nor `done/` move after 2 s ⇒ server raises "plugin not loaded?" error.

### Command schemas

`add_notes` - the core operation:
```json
{
  "id": 17, "op": "add_notes",
  "pattern": 3,            // 0-based FL pattern number; -1 = current   [Spike 1: explicit]
  "channel": 4,            // 0-based channel index; -1 = selected      [Spike 1: explicit]
  "flags": 0,              // bit 0: NPF_EmptyFirst, bit 1: NPF_UseSelection
  "notes": [
    {
      "pos": 0,            // PPQ ticks from pattern start
      "len": 96,           // PPQ ticks
      "key": 60,           // MIDI note number → TNoteParams.Note
      "vel": 100,          // 0..128 → TNoteParams.Vol
      "pan": 0,            // -64..64 → TNoteParams.Pan          [Spike 1: range]
      "color": 0,          // 0..15 note color / MIDI channel
      "pitch": 0,          // fine pitch → TNoteParams.Pitch     [Spike 1: units]
      "fcut": 0.0, "fres": 0.0
    }
  ]
}
```

Notes on units: `TNoteParams` positions/lengths are **PPQ ticks** [VERIFIED - header
comment]. FL's project PPQ is queryable (default 96; `general.getRecPPQ()`
[ASSUMED - validated Spike 3]) and reported in the control-plane `hello`, so the
**server** owns beats→ticks conversion; the spool speaks ticks only. Exact runtime
semantics of `Vol/Pan/Pitch` scaling are confirmed empirically in Spike 1 and the
findings recorded here.

Other v1 ops:

| op | Dispatcher | Notes |
|---|---|---|
| `add_notes` | `FHD_AddNotesToPR` | flags=NPF_EmptyFirst gives clear-and-write |
| `clear_notes` | `FHD_AddNotesToPR` with `Count=0, NPF_EmptyFirst` | [ASSUMED count=0 legal; else 1 dummy note + delete strategy - Spike 1] |
| `preview_note_on` / `preview_note_off` | `FHD_NoteOn`/`FHD_NoteOff` | audible preview, no writing |
| `load_sample` | `FHD_LoadInChannel` | absolute path in payload |
| `load_audio_clip` | `FHD_LoadAudioClip` | drops at playlist selection |
| `ping` | - | plugin liveness + version report |

Response:
```json
{ "id": 17, "ok": true, "notes_written": 12, "plugin_ver": "0.1.0" }
{ "id": 17, "ok": false, "err": "pattern 3 does not exist" }
```

### Concurrency & idempotency

- Single server instance per session (enforced via `session.json` pid + heartbeat).
- Commands are idempotency-keyed by `id`; the plugin records the last K processed ids
  and answers duplicates from memory without re-executing - safe retries.
- The plugin is single-threaded with respect to the spool (all on Idle), so no locking
  is needed on the FL side.

---

## Cross-plane coordination

Some tools need both planes in order (e.g. select channel via control plane, then
write via note plane if explicit `ChanNum` turns out unsupported). The **MCP server**
is the only orchestrator; neither in-FL component knows the other exists. Sequencing
rule: a cross-plane tool call completes plane 1, awaits its response, then issues
plane 2 - no speculative parallelism inside one tool call, ever. Simplicity beats
latency here; both planes are sub-50 ms.

### Amendment (spec 11 supersedes)

v0 ships the opposite of the units rule above: the spool carries **beats**, and
conversion to ticks happens inside FL where the score's real PPQ lives
(`flp.score.PPQ`). This removes the PPQ-handshake dependency and is the
standing design; the ticks-on-the-wire rule applies only if a future control
plane needs it. See docs/10 and docs/11.

Also note: a file-spool control plane (replacing SysEx entirely) is an open
question - see repo issue #8 and the addendum in docs/11.

---

## Protocol C: live master PCM stream

`stream_start {"port": N}` tells the Bridge to send fixed 1024-frame,
interleaved stereo signed-16-bit PCM chunks to `127.0.0.1:N`; `stream_stop`
ends that stream. The mixer callback copies into a bounded SPSC ring only.
A non-audio Bridge worker sends UDP packets, so disk, socket, and receiver
backpressure cannot block or allocate on FL's mixer thread. UDP is used because
the stream is local, latency-sensitive, and each packet has an explicit
sequence number and cumulative drop count: a lost packet is reported, never
silently joined to adjacent audio.

Each packet is little-endian `FLAU`, sequence `u32`, sample rate `u32`,
channels `u16`, frames `u16`, drops `u64`, followed by PCM. The engine writes
`bridge_audio.json` beside `bridge_udp.json`; it contains its loopback TCP
fanout endpoint. TCP clients receive the same header and PCM bytes.
`fl-mcp-listen --port-file <spool>/bridge_audio.json --seconds 5 out.wav`
records that endpoint. This transport is separate from command UDP and works
on macOS and Windows through the same loopback socket API.
