# 02 - Architecture

## Components

```
                    ┌─────────────────────────────────────────────┐
                    │                FL Studio (GUI)               │
                    │                                             │
 ┌─────────┐  MCP   │  ┌──────────────────┐   ┌────────────────┐  │
 │  Model  │◄──────►│  │ Controller script │   │  Bridge plugin │  │
 │ (Claude)│        │  │ device_FLMCP.py   │   │  (C++ effect,  │  │
 └─────────┘        │  │ (Python 3.9,      │   │   silent, on a │  │
      │             │  │  MIDI scripting)  │   │   mixer insert)│  │
      ▼             │  └───────▲──────────┘   └───────▲────────┘  │
 ┌─────────────┐    │          │ OnSysEx / SysEx out  │ Idle poll │
 │ MCP server  │    │          │                      │           │
 │ (Python     │    └──────────┼──────────────────────┼───────────┘
 │  3.11+,     │               │                      │
 │  external)  │      virtual MIDI loopback      spool directory
 │             │      "FLMCP Req"/"FLMCP Res"    (JSON files on disk)
 └──────┬──────┘               │                      │
        │                      │                      │
        ├── control plane ─────┘                      │
        ├── note plane ───────────────────────────────┘
        └── render plane ──► FL.exe /R (offline, saved copies)
```

Three planes, separated by **which mechanism can do the job** (see matrix in 01):

| Plane | Transport | In-FL endpoint | Capabilities |
|---|---|---|---|
| **Control** | SysEx over virtual MIDI loopback | Controller script | transport, mixer, channel/pattern selection, plugin params, step seq, UI, state read-back |
| **Note** | JSON files in a spool directory | Bridge plugin (polls on Idle) | write notes to any channel/pattern, clear-and-write, sample/clip loading, note preview |
| **Render** | OS process spawn | none (separate FL process) | bounce saved project copies to wav/mp3 |

### Why the note plane uses a file spool, not MIDI

The control plane must be MIDI - that's the only way to reach the embedded Python
interpreter. But the Bridge plugin is our own native code, and how commands reach *it*
is our choice. Candidates considered:

1. **File spool polled on Idle** (chosen). `FHD_WantIdle` with `Value=2` gives the
   plugin an idle callback **even when its editor is closed** [VERIFIED - header
   comment]. The plugin polls a spool directory each idle tick, executes pending
   commands, writes responses. No focus stealing, no MIDI routing configuration for the
   user, no 7-bit encoding of note data, trivially debuggable (the protocol is files
   you can read). Latency is one idle period - tens of milliseconds, imperceptible for
   "AI writes a bassline."
2. MIDI SysEx routed to the plugin (`FHD_WantMIDIInput`). Zero-file purity, but whether
   global MIDI input actually reaches a native *effect* plugin is undocumented
   [ASSUMED-false until tested], and it adds port setup burden. Revisit later as an
   optimization if file latency ever matters.
3. Relay through the controller script via `plugins.setParamValue` byte-lanes. Proven
   primitives, but ~bytes-per-frame bandwidth and gruesome. Rejected.

### Why the control plane is not also a file spool

The controller script *could* poll files too - but `OnIdle` budget is tight (the whole
script shares FL's event loop, ~20 ms cadence [VERIFIED]), filesystem access from the
embedded interpreter is less certain than `OnSysEx` (which is the documented,
Flapi-proven path), and SysEx gives us push semantics with no polling cost. MIDI in,
MIDI out is the boring, known-good route for the control plane.

## Data flow examples

### `write_notes` (note plane)

```
Model → MCP tool call: write_notes(pattern=3, channel="Bass", notes=[...])
MCP server:
  1. resolves "Bass" → channel index 4 (via manifest + live verification, see 06)
  2. control plane: ensures channel 4 selected  ── SysEx → controller script
     (only needed if ChanNum=-1 semantics; explicit ChanNum tested in Spike 1)
  3. note plane: writes spool/commands/000123.json
       {id, op:"add_notes", pat:3, chan:4, flags:0, notes:[{pos,len,key,vel,...}]}
Bridge plugin (next Idle tick):
  4. reads command file, builds packed TNotesParams (variable-length alloc)
  5. PlugHost->Dispatcher(HostTag, FHD_AddNotesToPR, 0, (intptr_t)&params)
  6. FHD_SetDirty; writes spool/responses/000123.json {id, ok:true}
  7. deletes/archives the command file
MCP server:
  8. sees response file (filesystem watcher), resolves the tool call
  9. updates shadow score (see State model)
User: sees notes appear in the piano roll / pattern block immediately.
```

### `set_plugin_param` (control plane)

```
Model → set_plugin_param(channel="Lead", param="Filter Cutoff", value=0.62)
MCP server → SysEx frame {id, op:"plugins.setParamValue", args:[0.62, 41, 4, ...]}
Controller script OnSysEx → executes plugins.setParamValue(...) → SysEx response {id, ok}
Knob moves on screen; sound changes if playing.
```

## State model: who owns the truth

**FL Studio owns the truth. The MCP server holds two derived views:**

1. **Live state cache** - names, counts, selection, param values. Readable on demand
   through the control plane (the Python API has getters for all of it). Cheap to
   refresh; never trusted for long.
2. **Shadow score** - the note contents of every pattern/channel the *server itself*
   wrote. Maintained because **no live surface can read notes back**
   [VERIFIED - gap in both Python API and SDK]. Write-through: every successful
   `write_notes`/`clear_notes` updates it.

The shadow score drifts if the human edits notes by hand. Two mitigations:

- **Declared convention** (template, 07): patterns are partitioned - the model works in
  its patterns; a human editing them accepts that the model's memory of those patterns
  goes stale.
- **Resync tool** (`sync_from_project`): control plane triggers Save (Ctrl+S
  via `ui` keyboard emulation), then the server parses the saved `.flp` with **PyFLP**
  and rebuilds the shadow score from ground truth. Explicit, user-visible (it saves
  their project), and only invoked as a tool call, never silently.

**Single-writer rule:** while a session is active, the MCP server is the only automated
writer. The offline plane (PyFLP writes, CLI renders) operates **only on saved copies**
under a different filename - never on the file FL has open. This kills the
clobber-on-save failure mode by construction.

## Threading and safety (the crash frontier)

This is where a naive implementation kills the user's project. Rules:

- The Bridge plugin does **all host-dispatcher work on the Idle callback** (GUI
  thread). Never from audio render callbacks, never from our own threads.
  `FHD_AddNotesToPR` mutates project data; GUI thread is the conservative context.
  [ASSUMED-safe; Spike 1 validates under playback]
- If the plugin ever needs its own threads (it shouldn't in v1 - spool I/O is fast),
  declare `FHD_SetThreadSafe` and honor `LockMix_Shared` per SDK docs. Prefer not to.
- Keep per-Idle work bounded: process at most N commands (default 4) per tick; a
  10,000-note write is chunked by the *server*, not the plugin.
- The controller script keeps `OnSysEx`/`OnIdle` handlers under ~1 ms typical; long
  operations are decomposed by the server into many small commands.
- The Bridge plugin registers as an **effect that passes audio through untouched**
  (straight buffer copy in its render). Users may drop it on the master or on a spare
  insert; either way it must be sonically inert and CPU-negligible.

## Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| FL not running / project closed | Control-plane heartbeat (ping every 5 s) times out | Server marks session down; tools return actionable error ("FL Studio not reachable - is the project open?") |
| Bridge plugin not loaded | Spool command unclaimed after T=2 s | Error names the exact fix ("load 'FL MCP Bridge' on any mixer insert") |
| Controller script not attached | SysEx request times out but spool works | Same pattern; error names the MIDI-settings fix |
| Command crashes/undefined behavior in plugin | Response file with `ok:false, error:...`; plugin never lets exceptions escape the dispatcher boundary | Server surfaces error; command is idempotent-keyed so safe to retry |
| Server restarts mid-session | Sequence numbers persisted in spool dir | On boot, server scans spool for unresponded commands, reconciles, resumes counter |
| Human edits collide with shadow score | Undetectable live (no note read-back) | `sync_from_project` tool; docs set expectation |
| Stale spool from a previous session | Session ID embedded in spool path | New session = new directory; old ones garbage-collected on server start |

## Explicitly rejected alternatives

- **Keystroke/`osascript` GUI automation** (karl-andres approach): focus-stealing,
  single-flight, fragile. The Bridge plugin exists precisely to delete this.
- **All-offline PyFLP writes with FL as a viewer**: requires close/reopen of the
  project per change - the opposite of realtime.
- **Depending on the Flapi package**: unmaintained, `main` mid-refactor. We vendor the
  *pattern* (two loopback ports, SysEx framing) in our own controller script/server.
- **`FPF_MIDIOut`-style virtual controller plugin instead of an effect**: keeps the
  generator restriction on `ChanNum` - defeats the whole point.
