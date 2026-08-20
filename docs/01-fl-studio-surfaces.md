# 01 — FL Studio's Programmable Surfaces

There is no single "FL Studio SDK." There are four distinct programmable surfaces plus
one third-party escape hatch. A fully functional MCP must combine three of them. This
doc is the research foundation everything else rests on.

## Surface 1: MIDI Controller Scripting API (embedded Python)

- Embedded **Python 3.9** interpreter inside FL Studio; scripts live in
  `.../Image-Line/FL Studio/Settings/Hardware/<Name>/device_<Name>.py` and attach to a
  MIDI input port via FL's MIDI settings. [VERIFIED]
- Event-driven: `OnInit`, `OnDeInit`, `OnMidiMsg`, `OnSysEx`, `OnIdle` (~every 20 ms),
  `OnRefresh`, `OnProjectLoad`, `OnDirtyChannel`, `OnDirtyMixerTrack`. [VERIFIED]
- Modules: `transport`, `mixer`, `channels`, `patterns`, `playlist`, `plugins`, `ui`,
  `device`, `general`, `arrangement`, `screen`, `launchMapPages`, plus helper modules
  `midi` and `utils`. [VERIFIED — FL Studio API Stubs]
- `OnIdle` runs on FL's event loop; slow handlers back up MIDI processing. Keep every
  callback fast. [VERIFIED — API Stubs callback docs]

**Can:** transport control; mixer volume/pan/mute/solo/routing/naming; select
channels/patterns; step-sequencer grid bits (`channels.setGridBit`); get/set
**parameters** of any loaded plugin including third-party VST/AU (`plugins` module);
window/UI control; undo; read names, counts, colors, selection state. [VERIFIED]

**Cannot:** write persistent piano-roll notes (real-time note triggers don't persist
unless FL is recording); create patterns; add channels; load plugins; render audio;
**read piano-roll note data**. [VERIFIED — repeatedly confirmed on Image-Line forums;
no such functions exist in the API]

**Reachable from outside?** Yes — this is the Flapi trick. The script listens on a
virtual MIDI loopback port; an external process sends commands as SysEx; the script
executes API calls and replies with SysEx on a second port. [VERIFIED — Flapi works
this way; BeatSage independently built the same thing]

## Surface 2: Piano Roll Scripting (`flpianoroll`, `.pyscript`)

- FL Studio 21+. Scripts live in `.../Settings/Piano roll scripts/`, appear in the
  piano roll's *Tools ▸ Scripting* menu. Full read/write access to the open score:
  `score.notes`, `Note` objects, markers. [VERIFIED]
- **Only runs when a human invokes it from the menu** (or its redo shortcut). Not
  callable from the controller-script interpreter (`flpianoroll` is not importable
  there) and not triggerable externally. [VERIFIED]

**Role in our design: none at runtime.** This is the surface the keystroke-hack
projects abuse. We keep it in mind only as a possible manual escape hatch for users.

## Surface 3: Native Plugin SDK (C++/Delphi) — the key surface

In-process plugins built against `fp_plugclass.h` get a two-way **dispatcher** channel
to the host. The plugin calls `PlugHost->Dispatcher(HostTag, FHD_xxx, Index, Value)`.

### The note-writing call [VERIFIED — verbatim from headers]

```c
// fp_plugclass.h:730
const int FHD_AddNotesToPR = 17;  // add a note to the piano roll, PNotesParams in Value
```

```c
// fp_plugclass.h — packed, variable-length
#pragma pack(push, 1)
typedef struct {
    int   Position;  // in PPQ
    int   Length;    // in PPQ
    int   Pan;       // default=0
    int   Vol;       // default=100/128
    short Note;      // default=60 (middle C)
    short Color;     // 0..15 (=MIDI channel / note color)
    int   Pitch;     // default=0
    float FCut;      // filter cutoff, default=0
    float FRes;      // filter Q, default=0
} TNoteParams;
#pragma pack(pop)

typedef struct {
    int Target;    // 0=step seq (not supported yet), 1=piano roll
    int Flags;     // see NPF_EmptyFirst
    int PatNum;    // -1 for current
    int ChanNum;   // -1 for plugin's channel, or selected channel if plugin is an effect
    int Count;     // # of notes in the structure
    TNoteParams NoteParams[1];  // variable-size array
} TNotesParams, *PNotesParams;

const int NPF_EmptyFirst   = 1;  // delete everything before adding the notes
const int NPF_UseSelection = 2;  // dump inside piano roll selection if any
```

### The discovery, precisely stated

The SDK's *narrative* docs (`gettingstarted/host.html`) say: *"With FHD_AddNotesToPR a
plugin can ask the host to add one or more notes to the piano roll **for the plugin's
channel**."* That sentence is why the world believes cross-channel writing is
impossible.

The *header* says the target is **"selected channel if plugin is an effect."**
Effects don't live on a channel at all — they live on the mixer — so the restriction
that pins a generator to its own channel doesn't apply. Register as
`FPF_Type_Effect` instead of `FPF_Type_FullGen`, install into the Effects plugin
folder, and `ChanNum` retargets. [Header semantics VERIFIED; actual cross-channel
write REPORTED by BeatSage; reproduction is **Spike 1**]

### Other dispatcher calls we will use [VERIFIED — all from fp_plugclass.h]

| Constant | # | Use in our design |
|---|---|---|
| `FHD_AddNotesToPR` | 17 | The write path |
| `FHD_NoteOn` / `FHD_NoteOff` | 20/21 | Audible note preview without writing |
| `FHD_NoteOnOff` | 56 | Generator-style live note on/off (if ever needed) |
| `FHD_SetThreadSafe` | 33 | Declare thread-safety discipline to host |
| `FHD_WantIdle` | 13 | **Value=2 → Idle called even with editor closed** — enables our spool polling |
| `FHD_LoadInChannel` | 43 | Load a sample file into selected channel(s) |
| `FHD_LoadAudioClip` | 42 | Drop an audio file into the playlist |
| `FHD_EditSample` | 32 | Open a sample in Edison (future) |
| `FHD_GetMixingTime` / `FHD_GetPlaybackTime` | 36/37 | Timing queries |
| `FHD_TicksToTime` | 16 | PPQ → bar:step:tick conversion |
| `FHD_GetPath` | 29 | Locate FL data paths for spool discovery |
| `FHD_OnHint_Direct` | 22 | Progress display in FL's hint bar |
| `FHD_DebugLogMsg` | 45 | Logging into FL's debug log |
| `FHD_SetDirty` | 48 | Mark project modified after writes |

### What the SDK still cannot do [VERIFIED — absence in headers]

- **Instantiate a plugin.** `FHD_ShowPlugSelector` (15) and `FHD_ShowPicker` (57) only
  open UI for a human to click.
- **Create a pattern.** `PatNum` addresses existing patterns only; no create call.
- **Render headlessly.** `FHD_RenderProject` (60) "prompts the rendering dialog."

### Platform notes

- Headers are guarded with `#ifdef __APPLE__` throughout — macOS is a first-class
  target, not a port. [VERIFIED]
- The SDK is distributed by Image-Line (forum download); the CMake template repo
  vendors it. **Check the SDK's redistribution terms before we vendor headers in a
  public repo.** [OPEN — legal check in roadmap]

## Surface 4: Command-line rendering

```
FL.exe /R [filename] /E<mp3,ogg,wav,flac> /F<source folder> /O<dest folder> /M /Z
```

Batch-renders `.flp` files to audio or MIDI without interactive use; available since
FL 12. [VERIFIED — Image-Line manual "Exporting Audio & MIDI"]. Caveats: paths with
spaces are problematic; GUI may still appear during render; requires a licensed FL
install. Role in our design: secondary — "bounce what we made" tool, and the future
offline-arena path.

## Escape hatch: PyFLP (third party)

Pure-Python read/write of `.flp` files, no FL Studio required. [VERIFIED] Role in our
design: **ground-truth read-back**. The Python API cannot read piano-roll notes, so
when the MCP server needs to verify or re-sync its shadow score (02), it asks FL to
save (`ui` keyboard shortcut for Ctrl+S via control plane), then parses the saved file
with PyFLP. Never used as a write path while FL has the project open (clobber risk).

## The capability matrix

| Task | Python API | PR pyscript | Native SDK | CLI | PyFLP (offline) |
|---|:--:|:--:|:--:|:--:|:--:|
| Transport / mixer / routing | ✅ | — | partial | — | ✅ |
| Plugin **param** get/set (incl. VST) | ✅ | — | own params | — | ~ |
| **Load** new VST/AU | ❌ | ❌ | ❌ (picker UI only) | — | ✅ |
| **Create pattern** | ❌ | ❌ | ❌ | — | ✅ |
| Write notes, live + persistent | ❌ | ⚠️ menu-only | **✅ `FHD_AddNotesToPR`** | — | ✅ (file) |
| Read notes | ❌ | ⚠️ menu-only | ❌ | — | ✅ (file) |
| Step sequencer | ✅ | — | — | — | ✅ |
| Render audio | ❌ | — | dialog only | ✅ | ❌ |

Read the rows as a routing table: every capability of the MCP server maps to exactly
one surface, chosen by this matrix. The composite system covers everything except
plugin loading and pattern creation — which the template project (07) converts from
runtime problems into setup-time conventions.
