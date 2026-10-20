# SDK reference

`fl_mcp.sdk` is the immediate Python API for one FL Studio session. Calls apply
on return and either yield confirmed state or raise; there is no queue or
`flush()` step.

The stable public surface is deliberately small:

- `fl.track(...).write(...)`, `clear()`, and `notes()` for channel/pattern notes.
- `fl.set_tempo(...)` for confirmed tempo changes.
- `fl.check(...)` to prove in seconds which channels sound.
- `fl.render(...)`, `fl.read_wav(...)`, and `fl.analyze(...)` for render/audio workflows.
- `fl.track(...).set_automation(...)` for existing FL-authored automation clips.
- `with fl.file_edit(): ...` to batch file-lane edits into a single FL reload in development mode.
- `fl.track(...).params(...)` and `set_param(...)` for live generator plugin parameters.

Notes always go to the channel named by `fl.track(...)`; live Bridge writes select and verify that
channel first when it is not already selected in FL.

Failures from FL/session boundaries raise `fl_mcp.errors.ToolError`, whose
`category`, `message`, `fix`, and `retryable` fields are intended to be read by
callers. Channel lookup failures raise `ChannelNotFound`, a `ValueError`, with
the current channel list in the message.

When `FLMCP_SPOOL_ROOT` is set, the session is running a live match and never
kills or reopens FL. `file_edit()` and every file-lane fallback raise retryable
`file_lane_requires_reload` before changing the project file. Use Bridge notes
and wire tempo, parameters, or channel levels instead. Automation clips and
adding effects have no live equivalent yet.

```python
from fl_mcp.sdk import connect
from fl_mcp.schemas import Note

fl = connect()
bass = fl.track("Bass")
bass.write([Note(key="C2", start=0, length=1)], mode="replace")
wav = fl.render("/tmp/bass-pass.wav")
analysis = fl.analyze(wav)
if analysis.true_peak_dbfs > -1:
    bass.write([Note(key="C2", start=0, length=1, velocity=0.7)], mode="replace")
```

## Connection

### `connect(session: FLSession | None = None) -> FL`

Creates the SDK entry point. With no argument it creates the default session;
passing a session is useful for dependency injection in an application or test.

```python
fl = connect()
```

## `FL`

### `fl.channels -> list[Channel]`

**Availability:** confirmed session snapshot (live FL when connected, otherwise
the configured project file). Returns channel index, name, normalized volume,
pan, and mute state.

```python
for channel in fl.channels:
    print(channel.index, channel.name)
```

### `fl.tempo -> float`

**Availability:** live FL when connected (so a `set_tempo` shows at once),
otherwise the project's saved tempo; raises when neither is known. Returns BPM.

```python
print(fl.tempo)
```

### `fl.track(name_or_index: str | int, pattern: int = 1) -> Track`

**Availability:** project-file coordinate lookup when a project is configured;
otherwise the session snapshot. `name_or_index` is a channel index, an exact
channel name, or an unambiguous case-insensitive substring. `pattern` is the
one-based piano-roll pattern number. Raises `ChannelNotFound` for no or
ambiguous match.

```python
lead_in_pattern_two = fl.track("Lead", pattern=2)
```

### `fl.set_tempo(bpm: float) -> float`

**Availability:** live control when connected, otherwise project write plus
reload. `bpm` is the requested tempo. Returns the confirmed BPM readback, which
may be quantized.

```python
confirmed_bpm = fl.set_tempo(128.0)
```

### `fl.render(output: str | Path) -> Path`

**Availability:** live session with the Master Bridge and FL audio output. Reads
the live arrangement's length from FL (the playlist in song mode, the selected
pattern in pattern mode, at the live tempo), plays it once from the start while
the Bridge records the mix, and returns the verified WAV path. Nothing is saved.
It raises before recording if song mode has an empty playlist or the capture
would pass ten minutes, and after recording if the WAV does not verify.
The `render` MCP tool also returns the span it recorded (`RenderSpan`).

A render plays in real time and produces the real mix, so render once when the
pass is done; the harness listens back after you say you are done. Use
`get_scene` to read work and `fl.check()` to prove it sounds instead.

```python
wav = fl.render("/tmp/mix.wav")
```

From the `run` tool only the file name is used: the WAV lands in the session's
read-only renders folder and the returned path says where ([38](38-python-kernel.md)).

### `fl.read_wav(path: str | Path) -> tuple[np.ndarray, int]`

**Availability:** local file read. Loads a PCM WAV as `(samples, sample_rate)`.
`samples` is a float64 NumPy array shaped `(frames, channels)`, normalized to
roughly `[-1, 1]`; `sample_rate` is Hz.

`render(path) -> Path` records the live mix through the Bridge and preserves the
confirmed artifact. Use `read_wav` only when raw NumPy samples are wanted, and
`analyze` when derived metrics are enough.

```python
samples, sample_rate = fl.read_wav(wav)
peak = abs(samples).max()
```

### `fl.analyze(wav: str | Path) -> AudioAnalysis`

**Availability:** local file analysis. Computes deterministic metrics from a
PCM WAV; it does not control FL.
It streams the file in fixed-size blocks, so its memory stays flat however long the
render (prefer it to `read_wav`, which loads every frame, for a full-length song).
Integrated loudness and tempo equal a whole-file pass; true peak is 4x band-limited
interpolation per block with real signal on both sides of each seam; key and spectral
balance come from the mean Hann-windowed spectrum of 65,536-frame segments (one
segment, so exactly the whole-file spectrum, for a render that short).

```python
analysis = fl.analyze("/tmp/mix.wav")
print(analysis.integrated_lufs, analysis.key)
```

### `fl.check(bars: int = 2, start_bar: int = 1) -> CheckResult`

**Availability:** live-only. Plays `bars` bars (1 to 8) from `start_bar` and
polls FL's own mixer meters (`mixer.getTrackPeaks`) for each channel's mixer
track and the master, keeping the loudest reading. FL is stopped afterwards
(stopped first if it was playing); no mute, solo or note changes. It takes
about the span's length in real time.

Each `ChannelCheck` has `name`, `sounded`, `peak_dbfs`, `mixer_track` and
`reason`. A silent channel names why when FL or the session's note record says
so: muted, its mixer track muted, zero volume,
or no notes in the bars played; otherwise it "made no sound". Channels sharing
a mixer track are measured together and say so. A channel routed straight to
the master has `sounded=None`: the master meter is the whole mix, so it cannot
be measured on its own. `summary` is one line to read.

```python
print(fl.check().summary)
# 4 of 5 channels sounded in bars 1-2; the master peaked at -3.1 dBFS.
# Silent: FLEX Bass (it has no notes in bars 1-2).
```

### `fl.undo() -> None`

**Availability:** live-only. Invokes FL's undo command. The session's note
record does not follow it, so write the track again to set its notes for certain.

## `Track`

A `Track` is the resolved channel and pattern returned by `fl.track(...)`. Its
public `channel: int` and `pattern: int` attributes are the resolved indices.

### `track.write(notes: list[Note], mode: NoteWriteMode = "merge") -> WriteResult`

**Availability:** live Bridge, or project write outside a match. `notes` are
musical notes to apply. `mode` is `"merge"` to add them or `"replace"` to
replace this target's notes. Returns `WriteResult(wrote, track, pattern, mode,
total)`: the notes written, the channel's name, and the target's confirmed note
count afterwards (see [docs/27 "Note state"](27-shared-surfaces.md#1a-note-state)).
`total` is None only for a merge onto a track the session never saw. A refusal or
timeout raises.

```python
result = fl.track("Bass").write(
    [Note(key="C2", start=0, length=1, velocity=0.85)], mode="replace"
)
```

### `track.clear() -> WriteResult`

**Availability:** as `write`. Removes all notes from this channel/pattern
target and returns the result (`total=0`).

```python
fl.track("Bass").clear()
```

### `track.notes() -> list[Note]`

**Availability:** always. Returns the notes FL has on this channel/pattern
target now: this session's confirmed writes over the project FL loaded.

```python
for note in fl.track("Bass").notes():
    print(note.key, note.start)
```

### `track.set_automation(points: list[AutomationPoint]) -> AutomationResult`

**Availability:** project write. Replaces this channel's existing automation
clip and returns points confirmed by reparsing the saved project. It requires
an existing FL-authored clip.

```python
fl.track("Bass").set_automation([
    AutomationPoint(position=0, value=0.2),
    AutomationPoint(position=4, value=0.8),
])
```

### `track.set_channel_levels(volume: float | None = None, pan: float | None = None, pitch: int | None = None) -> ChannelLevels`

**Availability:** project write. Updates one or more channel levels through
the project file, then returns the saved-file readback. `volume` is normalized
from 0 to 1, `pan` ranges from -1 (left) to 1 (right), and `pitch` is whole
semitones from -48 to 48. Pass at least one value.

```python
confirmed = fl.track("Bass").set_channel_levels(volume=0.9, pan=-0.15)
```

### `fl.add_effect(insert_index: int, plugin_name: str) -> tuple[Effect, ...]`

**Availability:** project write. Adds an effect using an FL-authored opaque
record and returns the saved-file effect-chain readback. Current evidence only
supports `fl.add_effect(0, "Fruity Parametric EQ 2")`: an empty Master slot 0.
Other plugins or insert placements refuse rather than synthesize plugin state.

```python
effects = fl.add_effect(0, "Fruity Parametric EQ 2")
```

### `track.params(query: str | None = None, limit: int = 40) -> list[PluginParam]`

**Availability:** live-only. Reads the generator plugin's parameters. `query`
filters names by case-insensitive substring; `limit` caps the number returned.
If the live control plane is unavailable, this raises a retryable `ToolError`
instead of falling back to project-file state, because plugin state has no
honest file representation.

```python
cutoffs = fl.track("Lead").params(query="cutoff", limit=8)
```

### `track.set_param(param: int | str, value: float | str) -> PluginParam`

**Availability:** live-only. The portable generic path for a generator plugin
is to enumerate its live parameter surface, then pass one returned `index` to
`set_param`. That index is valid only for the generator currently loaded on
that track; enumerate again after replacing the plugin. This works wherever FL
exposes parameters, without reading or writing the plugin's opaque state blob.

`param` may also be a unique exact/substr name for convenience. Invalid, stale,
or out-of-range indexes fail before FL is asked to write. `value` is a finite
normalized number from 0 to 1, or - when this exact plugin has a captured
parameter model - its displayed numeric value with unit or an option name (an
option not in `PluginParam.options` is rejected before the write). Returns FL's
confirmed readback, which may be quantized to the plugin's control grid.
Unknown or ambiguous names raise a `ToolError` whose message includes the
matching names FL reported.

```python
lead = fl.track("Lead")
cutoff = lead.params(query="cutoff", limit=8)[0]
confirmed = lead.set_param(cutoff.index, 0.55)
# or, with a captured model for this exact plugin:
confirmed = lead.set_param("filter mode", "Bandpass")
```

## Data shapes

All shapes below are Pydantic models; construct them directly or validate input
with `Model.model_validate(...)`.

| Shape | Fields |
| --- | --- |
| `Note` | `key: int` (MIDI 0–127; note names such as `"C4"` are accepted on construction), `start: float` (beats), `length: float` (beats, >0), `velocity: float` (0–1, default .8), `pan: float` (0–1, default .5), `color: int` (0–15, default 0) |
| `NoteWriteMode` | Literal `"merge"` or `"replace"` |
| `AutomationPoint` | `position: float` (beats, >=0), `value: float` (normalized, -0.001–1.001), `tension: float` (-1–1, default 0) |
| `ChannelLevels` | `channel: int`, `volume: float` (0–1), `pan: float` (-1–1), `pitch_semitones: int` (-48–48) |
| `Effect` | `name: str`, `chunk: bytes` (opaque FL-authored plugin state) |
| `AudioAnalysis` | `bpm: float | None`, `key: str | None`, `integrated_lufs: float`, `true_peak_dbfs: float`, `spectral_balance: SpectralBalance` (`low_dbfs`, `mid_dbfs`, `high_dbfs`) |
| `PluginParam` | `index: int`, `name: str`, `value: float` (normalized 0–1), `display: str`, `kind: "continuous" | "stepped" | "enum" | "bool"`, `options: list[str] | None`, `minimum`, `maximum`, `step`, `unit` |
