# 04 - The Bridge Plugin (native C++ effect)

> **SUPERSEDED (2026-08-26):** FL 2026's native `ilmcp.runPRScript` replaces this plugin as the note-write path.
> See [12-fl2026-native-surface.md](12-fl2026-native-surface.md).
> Kept as historical record; the plugin survives only as a fallback until the doc-12 gating probe passes.

The load-bearing component. A silent FL-native effect whose sole purpose is to execute
spool commands against the host dispatcher. Everything risky in this project lives
here, which is why Spike 1 builds this first.

## Identity & registration

- **Name**: "FL MCP Bridge". Version reported in every spool response.
- **Type**: `FPF_Type_Effect` (`= FPF_NewVoiceParams`) - *not* a generator. This is the
  entire trick: effects get `ChanNum` retargeting on `FHD_AddNotesToPR`
  (see 01). [Header VERIFIED / behavior Spike 1]
- **Audio**: pass-through. `Eff_Render` copies input to output untouched. We do **not**
  set `FPF_NoProcess` initially - an effect that opts out of processing may interact
  oddly with hosting; a memcpy is free. [Revisit after Spike 1.]
- **Editor**: minimal or none at first. If FL requires an editor window, show a static
  status panel (connection state, session id, last command). No knobs, no parameters
  in v1 - parameters invite automation writes we'd have to handle.
- **Idle**: on init, `Dispatcher(FHD_WantIdle, 0, 2)` → idle callback **always**, even
  with editor closed. [VERIFIED header: "2 for always enabled"]
- **Placement**: any mixer insert; template project (07) pre-loads it on a dedicated
  utility insert so users never think about it.

## Install locations

Native FL plugins are discovered from FL's plugin directories, not the VST paths:

- Windows: `<FL install>\Plugins\Fruity\Effects\FL MCP Bridge\FL MCP Bridge_x64.dll`
- macOS: `/Applications/FL Studio 2025.app/.../Plugins/Fruity/Effects/...` bundle
  layout [ASSUMED - exact per-version paths and whether a user-writable location
  exists confirmed during Spike 1; the installer owns this knowledge]
- **The folder matters semantically**: installing under `Effects` vs `Generators` is
  part of what makes FL treat the plugin as an effect. [REPORTED - BeatSage relocation
  detail]

macOS additionally requires the dylib/bundle be **code-signed and notarized** for
distribution (dev builds run with quarantine removed). Budgeted in roadmap.

## Core loop

```
OnIdle (GUI thread, every tick):
  if not session_bound:
      session_bound = discover_spool()          # newest session.json w/ fresh heartbeat
      return
  for cmd in next_commands(max=4):              # lexical order
      resp = execute(cmd)                       # never throws across this boundary
      write_atomic(responses/<id>.json, resp)
      move(cmd, commands/done/)
```

All dispatcher calls happen here, on the GUI thread. No plugin-owned threads in v1.
Per-tick budget: if `execute` of one command exceeds ~5 ms (huge note array), that's a
server-side chunking bug; the plugin still completes it but logs a warning via
`FHD_DebugLogMsg`.

## Executing `add_notes`

```c
// Variable-length, byte-packed allocation - TNoteParams is #pragma pack(1)
size_t bytes = offsetof(TNotesParams, NoteParams) + count * sizeof(TNoteParams);
PNotesParams p = (PNotesParams)calloc(1, bytes);
p->Target  = 1;                    // piano roll
p->Flags   = flags;                // NPF_EmptyFirst / NPF_UseSelection
p->PatNum  = pattern;              // -1 = current; explicit values Spike 1
p->ChanNum = channel;              // -1 = selected (we are an effect); explicit values Spike 1
p->Count   = count;
for (int i = 0; i < count; i++) {
    TNoteParams* n = &p->NoteParams[i];
    n->Position = notes[i].pos;    // PPQ
    n->Length   = notes[i].len;    // PPQ
    n->Pan      = notes[i].pan;    // default 0
    n->Vol      = notes[i].vel;    // default 100/128 per header
    n->Note     = notes[i].key;    // default 60
    n->Color    = notes[i].color;  // 0..15
    n->Pitch    = notes[i].pitch;
    n->FCut     = notes[i].fcut;
    n->FRes     = notes[i].fres;
}
intptr_t r = PlugHost->Dispatcher(HostTag, FHD_AddNotesToPR, 0, (intptr_t)p);
PlugHost->Dispatcher(HostTag, FHD_SetDirty, 0, 0);
free(p);
```

Alignment traps: the struct is `#pragma pack(1)` with `short` members - **never** cast
arrays; always fill fields through the packed struct. `offsetof` for the header size
(not `sizeof(TNotesParams) - sizeof(TNoteParams)`, which double-counts padding
assumptions).

## Channel & pattern targeting - the decision tree

Spike 1 answers, in order:

1. **Does explicit `ChanNum = k` write to channel k** when registered as an effect?
   If yes → the note plane is fully self-contained. (BeatSage's "notes written to
   different channels" suggests yes. [REPORTED])
2. If only `ChanNum = -1` (selected channel) works → cross-plane orchestration:
   control plane `channels.selectOneChannel(k)` first, then spool write. Costs one
   round trip; still no GUI hacks.
3. **Does explicit `PatNum = k` write into non-current patterns**, including patterns
   never opened in the piano roll? If only `-1` works → control plane
   `patterns.jumpToPattern(k)` first. Also verify what "current pattern" means when the
   piano roll is closed.
4. `NPF_EmptyFirst` scope: does it clear the whole pattern or just the target channel's
   notes in it? Determines whether `clear_notes` is channel-safe.

The protocol (03) already carries explicit `pattern`/`channel` fields, so whichever
branch wins, the wire format doesn't change - only the server's orchestration does.

## Error containment

- Top-level `try`/OS-level guard in every Idle: an exception must never propagate into
  FL. On caught failure: `ok:false` response with message; plugin stays alive.
- Unknown `op` or newer `proto_ver` than supported → explicit error response, not
  silence (server gets to distinguish "old plugin" from "dead plugin").
- If the dispatcher returns an error/0 for a note write, report it verbatim; do not
  retry inside the plugin (the server owns retry policy via idempotent ids).

## Build

- Base: the official FL SDK headers (`fp_plugclass.h`, `fp_def.h`, `fp_cplug.h`) via
  the [CMake template](https://github.com/Barsay/FL-Studio-SDK-CMake-Template)
  structure. **Check Image-Line's SDK license before vendoring headers into a public
  repo; if redistribution is disallowed, the build fetches the SDK from the official
  source as a setup step.** [OPEN]
- Toolchains: MSVC x64 (Windows), clang universal binary arm64+x86_64 (macOS).
- One external dep at most (a JSON parser - `nlohmann/json` single header, or a
  hand-rolled subset; the schema is tiny and flat, so hand-rolled is acceptable to
  keep the binary dependency-free).
- CI builds both platforms from day one; artifacts are exactly what the installer
  ships.

## What this plugin deliberately does NOT do

- No MIDI I/O, no parameters, no state saved into the project beyond FL defaults -
  a project that contains the Bridge but is opened on a machine without it must load
  cleanly with just a "plugin missing" placeholder.
- No file writes outside the spool. No network. (Keeps the trust story auditable: the
  only capabilities are "read commands from a user-owned folder, write notes into the
  open project".)
- No knowledge of MCP, models, or the controller script. It executes spool ops. That's
  the whole contract, and it's what makes the component testable with `echo` and `cat`.
