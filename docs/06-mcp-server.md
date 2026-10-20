# 06 - The MCP Server

The external process. Owns all orchestration, all musical abstraction, and the tool
surface the model sees. Python 3.11+, official `mcp` SDK, `python-rtmidi` (or `mido`
over rtmidi) for MIDI, `watchdog` for spool watching, `pyflp` for resync.

## Design stance

**The tools speak music, the planes speak plumbing.** The model should say
"write these notes on Bass at pattern 3, bar 2" - not compute PPQ ticks or channel
indices. Conversions the server owns:

- **Time**: tools accept beats (floats) and bar:beat positions; server converts to PPQ
  ticks using the `hello`-reported PPQ. Ticks never appear in tool schemas.
- **Addressing**: tools accept channel/track/pattern **names** (with index fallback);
  the server resolves via the template manifest (07) + live `state.snapshot`, and
  returns did-you-mean errors on ambiguity.
- **Velocity/pan**: normalized 0..1 / -1..1 in tool schemas; server maps to FL ranges.

## Tool catalog (v1)

Grouped as the model will perceive them. Every tool returns rich, actionable errors
(see Error model). Names final; schemas sketched.

### Session
| Tool | Signature → returns |
|---|---|
| `fl_doctor` | `()` → status of both planes, versions, FL version, PPQ, template detected? Actionable fix list per failed check. |
| `get_session_state` | `()` → tempo, playing?, current pattern, channels[], mixer tracks[], pattern names/lengths. (Cached ≤2 s; `refresh=true` forces.) |
| `sync_from_project` | `()` → triggers Save, PyFLP-parses the file, rebuilds shadow score. Returns per-pattern note counts. Explicit, never automatic. |

### Notes (note plane)
| Tool | Signature |
|---|---|
| `write_notes` | `(pattern, channel, notes: [{start_beat, length_beats, key, velocity?, pan?, color?}], mode: "merge"\|"replace")` - `replace` uses `NPF_EmptyFirst` semantics per Spike 1 findings |
| `clear_notes` | `(pattern, channel)` |
| `get_notes` | `(pattern, channel?)` → from **shadow score**, flagged `source: "shadow"` with staleness caveat, or `source: "project"` right after a sync |
| `preview_notes` | `(channel, notes, tempo_synced: bool)` → audible preview via `FHD_NoteOn/Off` scheduling, writes nothing |

### Composition helpers (server-side pure functions - no FL round trip)
| Tool | Purpose |
|---|---|
| `chord_to_notes` | `("Am7", start_beat, length, voicing?)` → note arrays the model can pass to `write_notes`; keeps music theory out of wire round trips |
| `quantize_notes` / `humanize_notes` | grid + swing / timing + velocity jitter transforms on note arrays |

### Transport & arrangement (control plane)
`play`, `stop`, `record`, `set_tempo(bpm)`, `set_song_position(bar, beat)`,
`select_pattern(name_or_index)`, `set_pattern_name`, `set_loop_mode("pattern"|"song")`.

### Channels & step sequencer
`list_channels`, `select_channel`, `set_channel_volume/pan`, `mute_channel`,
`set_step_grid(channel, pattern, steps: [bool] | "x---x---x---x---" notation)`,
`load_sample(channel, path)`.

### Mixer
`list_mixer_tracks`, `set_track_volume/pan`, `mute_track`, `solo_track`,
`name_track`, `route_track(src, dst, on)`.

### Sound design
| Tool | Notes |
|---|---|
| `list_plugin_params` | `(target)` → paginated `{idx, name, value}`; VSTs can have thousands - server caches the name table per plugin identity |
| `get_plugin_param` / `set_plugin_param` | by param name (resolved) or index; values normalized 0..1 |
| `set_plugin_preset` | next/prev/index navigation via `plugins` module |

### Render (render plane)
| Tool | Notes |
|---|---|
| `render_project` | `(format: wav\|mp3\|flac, out_path?)` → saves a **copy** of the project (via sync save + file copy), spawns `FL.exe /R` on the copy, returns artifact path. Requires configured FL executable path. Marked slow (seconds–minutes). |

Deliberately **absent**: `load_plugin`, `create_pattern`, `add_channel` - impossible
live (01). The tool descriptions for adjacent tools mention the template convention so
the model learns the boundary instead of flailing: e.g. `write_notes` errors on an
unknown channel with *"No channel 'Piano'. Available: Kick, Snare, Hat, Bass, Lead,
Pad, Keys… (channels are fixed by the template - pick one or ask the user to load an
instrument)"*.

## Shadow score

- In-memory + persisted to disk per project (keyed by project file path + FL project
  GUID if obtainable), so server restarts don't lose it.
- Updated write-through on every successful `write_notes`/`clear_notes`.
- Every `get_notes` response carries provenance (`shadow` vs `project`) and the
  timestamp of last sync, so the model can reason about staleness and proactively call
  `sync_from_project` when the user says they edited by hand.

## Error model

Tool errors are structured and always name the fix:

```json
{ "error": "note_plane_unavailable",
  "message": "The FL MCP Bridge plugin didn't pick up the command within 2s.",
  "fix": "In FL Studio: open the mixer (F9), select the 'MCP' insert, and confirm 'FL MCP Bridge' is loaded. Run fl_doctor after.",
  "retryable": true }
```

Categories: `control_plane_unavailable`, `note_plane_unavailable`, `unknown_target`
(with candidates), `fl_api_error` (verbatim from script), `kernel_unavailable`
(the `run` kernel could not start or failed its boundary probe, docs/38), `unsupported_in_live_session`
(the two walls, with template guidance), `render_failed`.

## Startup sequence

1. Create/verify MIDI loopback ports (macOS: create; Windows: verify loopMIDI/WMS).
2. Create spool session dir, write `session.json`, start heartbeat + GC of dead sessions.
3. Await controller-script `hello` (or prompt user to open FL / enable script).
4. `ping` the Bridge plugin through the spool.
5. `state.snapshot` → warm cache; detect template manifest; load persisted shadow score.
6. Expose tools. `fl_doctor` re-runs 1–5 on demand.

## Security & trust boundaries

- The server executes **no arbitrary code in FL** - the script dispatch is
  whitelisted (05); the plugin executes a fixed op set (04).
- Model code from `run` executes in a separate kernel process confined by the OS
  to its workspace (an AppContainer on Windows), never in the server (docs/38).
- Filesystem: spool dir + project saves + render outputs only; sample loading takes
  absolute paths but the tool schema documents that it reads user files (hosts can
  gate it with permissions).
- MIDI ports are named and dedicated; frames without our magic are ignored, so a
  stray controller on the same port can't inject ops.
- No network access anywhere in the in-FL components; the only network surface is the
  MCP transport itself (stdio by default).
