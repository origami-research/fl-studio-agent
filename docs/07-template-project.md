# 07 - The Template Project

> **PARTIALLY SUPERSEDED (2026-08-26):** FL 2026's `ilmcp.addEffect` / `ilmcp.addChannel` make plugin loading and instrument creation programmatic.
> The template's remaining job is the pattern bank only (pattern creation still has no API).
> See [12-fl2026-native-surface.md](12-fl2026-native-surface.md).

Two things are impossible in a live session, from any surface (01):

1. **Loading a new plugin** - the picker is human-only.
2. **Creating a new pattern** - no API/dispatcher call exists.

The template project converts both from runtime failures into setup-time conventions.
This mirrors how producers actually work - most open a personal template, not an empty
project - so the constraint costs almost nothing in practice and improves model
behavior (a fixed instrument palette beats an agent guessing VST paths).

## Contents of `FLMCP-Template.flp`

### Channel rack (the instrument palette)

| # | Name | Instrument | Role |
|---|---|---|---|
| 0–3 | `Kick`, `Snare`, `Clap`, `Hat` | Stock one-shots (FPC or sampler channels) | Drums - step-seq friendly |
| 4 | `Perc` | Sampler | Auxiliary percussion |
| 5 | `808` | Stock 3xOsc/FLEX preset | Sub bass |
| 6 | `Bass` | FLEX or user's synth | Melodic bass |
| 7 | `Keys` | FLEX piano/EP | Chords |
| 8 | `Lead` | FLEX lead | Melody |
| 9 | `Pad` | FLEX pad | Atmosphere |
| 10–15 | `User1`–`User6` | empty sampler channels | User loads anything; model can `load_sample` into them |

Stock-plugin-only in the shipped template (everyone has them). Users are encouraged to
save their own variant with their VSTs - the manifest (below) is regenerated, nothing
else changes.

### Patterns

`P01`–`P32` pre-created, empty, 16 bars max length each [length semantics per Spike 1
findings on `PatNum`/pattern length interaction]. Convention taught to the model via
tool docs: patterns are the model's workspace; renaming (`set_pattern_name`) is how it
labels sections ("Verse Drums", "Hook Bass").

Pattern 1 is also placed on playlist track 1 from beat 0 through the default 16-beat
loop. The default note-write target is Pattern 1, so its notes are audible when FL
renders the arrangement. This is a template invariant, not a render-time mutation.

### Mixer

- Named inserts: `Drums`, `Bass`, `Music`, `FX`, and a utility insert named `MCP`.
- Channels pre-routed to their group inserts.
- **`FL MCP Bridge` pre-loaded on the `MCP` insert** - the single most error-prone
  install step, eliminated.
- Basic send chain (reverb/delay on sends) so `set_plugin_param` has something
  expressive to grab immediately.

### Controller script

Enabling `device_FLMCP.py` in MIDI settings is per-FL-install (not per-project), so the
template can't carry it - the installer + `fl_doctor` own that step.

## The manifest

`FLMCP-Template.manifest.json`, shipped next to the `.flp` and regenerable from any
project via `sync_from_project`:

```json
{
  "template_ver": 1,
  "channels": [
    {"idx": 0, "name": "Kick", "kind": "drum",  "mixer_track": 1},
    {"idx": 6, "name": "Bass", "kind": "synth", "mixer_track": 2,
     "plugin": "FLEX", "notable_params": {"cutoff": 41, "resonance": 42}}
  ],
  "patterns": {"count": 32, "naming": "P01..P32"},
  "mixer": [{"idx": 1, "name": "Drums"}, {"idx": 2, "name": "Bass"}]
}
```

Purpose: gives the server (and through it, the model) name→index resolution and a
curated map of *musically meaningful* parameters per instrument, without a live
round trip. Live state always wins on conflict (`state.snapshot` cross-check at
startup; mismatches surface in `fl_doctor` as "project drifted from manifest -
regenerate?").

## Custom templates

`fl_doctor` treats "template" as: any project + matching manifest. Users make their
own: open their project, load Bridge on an insert, run the `generate_manifest` server
command (PyFLP parse of the saved file → manifest skeleton → user labels `kind` and
`notable_params`). Power users get their whole VST rack under model control this way -
loading happened at human time, control happens at model time.

## Distribution

The session template ships as package data at `fl_mcp/assets/session-template.flp`.
Consumers must resolve it with `fl-mcp-template`, never copy it: this is the one
canonical artifact used by the engine and its dev session. Its SHA-256 is
`ff110f543ab6d41557d2eba0500a0fd6d715f861b3a877757a0d424cabf9a669`.
