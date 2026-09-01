# SDK reference

`fl_mcp.sdk` is the immediate Python API for one FL Studio session. Calls apply
on return and either yield confirmed state or raise; there is no queue or
`flush()` step.

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

**Availability:** confirmed session snapshot; raises when tempo is not known.
Returns BPM.

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

**Availability:** live-only. Exports the current live project to `output` and
returns the confirmed WAV path; raises if FL cannot export or no artifact
appears.

```python
wav = fl.render("/tmp/mix.wav")
```

### `fl.read_wav(path: str | Path) -> tuple[np.ndarray, int]`

**Availability:** local file read. Loads a PCM WAV as `(samples, sample_rate)`.
`samples` is a float64 NumPy array shaped `(frames, channels)`, normalized to
roughly `[-1, 1]`; `sample_rate` is Hz.

`render(path) -> Path` is the honest primary primitive: it maps to FL's
`ui.exportWav` and preserves the rendered artifact. Use `read_wav` only when
raw NumPy samples are wanted, and `analyze` when derived metrics are enough.

```python
samples, sample_rate = fl.read_wav(wav)
peak = abs(samples).max()
```

### `fl.analyze(wav: str | Path) -> AudioAnalysis`

**Availability:** local file analysis. Computes deterministic metrics from a
PCM WAV; it does not control FL.

```python
analysis = fl.analyze("/tmp/mix.wav")
print(analysis.integrated_lufs, analysis.key)
```

### `fl.play() -> None`, `fl.stop() -> None`, `fl.undo() -> None`

**Availability:** live-only. Start transport, stop transport, or invoke FL's
undo command respectively.

```python
fl.play()
fl.stop()
fl.undo()
```

### `fl.snapshot() -> SessionState`

**Availability:** live snapshot when connected, otherwise a project-file
snapshot. Returns one confirmed backend view; it never combines live and file
state in the same result.

```python
state = fl.snapshot()
print(state.playing, state.current_pattern)
```

## `Track`

A `Track` is the resolved channel and pattern returned by `fl.track(...)`. Its
public `channel: int` and `pattern: int` attributes are the resolved indices.

### `track.write(notes: list[Note], mode: NoteWriteMode = "merge") -> WriteReceipt`

**Availability:** project write. `notes` are musical notes to apply. `mode` is
`"merge"` to add them or `"replace"` to replace this target's notes. Returns a
receipt with backend, target, confirmed note count, and delta.

```python
receipt = fl.track("Bass").write(
    [Note(key="C2", start=0, length=1, velocity=0.85)], mode="replace"
)
```

### `track.clear() -> WriteReceipt`

**Availability:** project write. Removes all notes from this channel/pattern
target and returns the confirmed receipt.

```python
fl.track("Bass").clear()
```

### `track.notes() -> list[Note]`

**Availability:** recorded/project-file state. Returns notes confirmed for this
channel/pattern target.

```python
for note in fl.track("Bass").notes():
    print(note.key, note.start)
```

### `track.set_automation(points: list[AutomationPoint]) -> AutomationReceipt`

**Availability:** project write. Replaces this channel's existing automation
clip and returns points confirmed by reparsing the saved project. It requires
an existing FL-authored clip.

```python
fl.track("Bass").set_automation([
    AutomationPoint(position=0, value=0.2),
    AutomationPoint(position=4, value=0.8),
])
```

### `track.params(query: str | None = None, limit: int = 40) -> list[PluginParam]`

**Availability:** live-only. Reads the generator plugin's parameters. `query`
filters names by case-insensitive substring; `limit` caps the number returned.

```python
cutoffs = fl.track("Lead").params(query="cutoff", limit=8)
```

### `track.set_param(param: int | str, value: float | str) -> PluginParam`

**Availability:** live-only. `param` is an index or a unique exact/substr name;
`value` may be normalized from 0 to 1, or (when this exact plugin has a
captured parameter model) its displayed numeric value with unit or an option
name. Returns FL's confirmed readback, which may be quantized to the plugin's
control grid. An option not present in `PluginParam.options` is rejected before
the write.

```python
confirmed = fl.track("Lead").set_param("filter mode", "Bandpass")
```

## Data shapes

All shapes below are Pydantic models; construct them directly or validate input
with `Model.model_validate(...)`.

| Shape | Fields |
| --- | --- |
| `Note` | `key: int` (MIDI 0–127; note names such as `"C4"` are accepted on construction), `start: float` (beats), `length: float` (beats, >0), `velocity: float` (0–1, default .8), `pan: float` (0–1, default .5), `color: int` (0–15, default 0) |
| `NoteWriteMode` | Literal `"merge"` or `"replace"` |
| `AutomationPoint` | `position: float` (beats, >=0), `value: float` (normalized, -0.001–1.001), `tension: float` (-1–1, default 0) |
| `AudioAnalysis` | `bpm: float | None`, `key: str | None`, `integrated_lufs: float`, `true_peak_dbfs: float`, `spectral_balance: SpectralBalance` (`low_dbfs`, `mid_dbfs`, `high_dbfs`) |
| `PluginParam` | `index: int`, `name: str`, `value: float` (normalized 0–1), `display: str`, `kind: "continuous" | "stepped" | "enum" | "bool"`, `options: list[str] | None`, `minimum`, `maximum`, `step`, `unit` |
