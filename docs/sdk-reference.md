# SDK reference

`fl_mcp.sdk` is the immediate Python API for one FL Studio session. Calls apply
on return and either yield confirmed state or raise; there is no queue or
`flush()` step.

The stable public surface is deliberately small:

- `fl.track(...).write(...)`, `clear()`, and `notes()` for channel/pattern notes.
- `fl.set_tempo(...)` for confirmed tempo changes.
- `fl.check(...)` to prove in seconds which channels sound.
- `fl.render(...)` and `fl.read_wav(...)` for render/audio workflows: the model measures and plots the samples itself.
- `fl.scene(...)`, `fl.mixer` and `fl.stock_folder` for what FL has, as plain objects.
- `fl.track(...).set_automation(...)` for existing FL-authored automation clips.
- `with fl.file_edit(): ...` to batch file-lane edits into a single FL reload in development mode.
- `fl.track(...).params(...)` and `set_param(...)` for live generator plugin parameters.
- `fl.route(...)` and `fl.send(...)` for live mixer routing and sends.
- `fl.presets(...)` and `fl.set_preset(...)` for a plugin channel's presets.
- `fl.sounds(...)` to search the stock presets and samples FL ships.
- `fl.load_sample(...)` and `fl.load_preset(...)` to load one of them into a channel.

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
samples, rate = fl.read_wav(fl.render("/tmp/bass-pass.wav"))
if abs(samples).max() > 0.9:
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
pan, mute state, and `about`: what the project's own notes (FL's Project info)
say the channel is for, from a line `<channel name>: <text>`; `None` when they
say nothing ([07](07-template-project.md)).

```python
for channel in fl.channels:
    print(channel.index, channel.name, channel.about)
```

### `fl.tempo -> float`

**Availability:** live FL when connected (so a `set_tempo` shows at once),
otherwise the project's saved tempo; raises when neither is known. Returns BPM.

```python
print(fl.tempo)
```

### `fl.track(name_or_index: str | int, pattern: int = 1) -> Track`

**Availability:** the channels `fl.channels` lists: FL's live names when FL is
connected, otherwise the project's saved names.
`name_or_index` is a channel index, an exact channel name, or an unambiguous
case-insensitive substring.
A channel FL renamed (a loaded sample or preset, a preset step, a rename by
hand) resolves by its new name at once.
Every method that takes a channel (`fl.route`, `fl.presets`, `fl.set_preset`,
`fl.load_sample`, `fl.load_preset`) resolves it here.
`pattern` is the one-based piano-roll pattern number.
Raises `ChannelNotFound` for no or ambiguous match.

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

### `fl.render(output, *, bars=None, start_bar=1, solo=(), mute=(), stems=None) -> Render | list[Render]`

**Availability:** live session with the Master Bridge and FL audio output. Reads
the live arrangement's length from FL (the playlist in song mode, the selected
pattern in pattern mode, at the live tempo), plays it while the Bridge records
the mix, and returns the verified WAV. Nothing is saved.
It raises before recording if song mode has an empty playlist or the capture
would pass ten minutes, and after recording if the WAV does not verify.

It returns a `Render`: the WAV's `Path` (so `fl.read_wav`, `open` and soundfile take it) that also says what played.
`.span` is the `RenderSpan`: `start_bar`, `beats`, `tempo_bpm` and `seconds`, so model code marks bars itself (bar `n` of the render starts `(n - start_bar) * 240 / tempo_bpm` seconds in, at four beats a bar); `.solo` and `.mute` are the channels it was rendered with.
The WAV runs 0.5 s past `seconds`: the decay tail, or the start of the next bar for a range.
Until 2026-11-03 it returned a plain `Path`.

Since 2026-11-03 it can record part of the song or some of its channels, so the model can listen to an instrument or a section on its own, and audition a change: write a test note, render that channel solo, measure it, then write the track back (`mode="replace"`), not `fl.undo()`, because FL's undo history holds the render's mute changes (see `fl.undo`):

| Argument | Effect |
| --- | --- |
| `bars`, `start_bar` | Record `bars` bars from `start_bar` (four beats a bar), cut at the song's end; a start past the end is refused. FL plays from that bar in absolute ticks, as `fl.check` does; notes held over from before it are not re-struck. |
| `solo` | Only these channels are heard (names or indices, as for `fl.track`; one alone is fine). |
| `mute` | These channels are silenced. |
| `stems` | `"channels"`: each channel alone; `"mixer"`: the channels routed to each mixer track together. One play per stem, so it takes that many times as long, bounded at ten minutes of recording in all; files are named `<output>-<label>.wav`. Returns the list. |

Solo and mute set each channel's mute in FL's channel rack explicitly and read it back, then set back exactly the channels they changed, also when the render fails; a channel FL will not set back is an error naming it.
FL's own channel solo is not used, because un-soloing unmutes every channel and would lose the user's mutes.
A muted channel is silent and everything else stays, so a soloed channel is heard through its whole mixer routing and effects, as in the mix.
A silent whole-song render is refused, because it means FL's audio path is broken; a silent range, solo or stem is returned as the answer.

The `render` MCP tool records the whole song as before and returns the WAV's `path`, its size in `bytes`, and the span it recorded.
The audio stays in the file: a person or client opens it at `path`, and model code reads it with `fl.read_wav`.
Until 0.1.5 the result also carried the WAV as base64, which overflowed the model's context in real clients.

A whole-song render plays in real time and produces the real mix, so render it once when the
pass is done; the harness listens back after you say you are done. Use
`get_scene` to read work and `fl.check()` to prove it sounds instead.

```python
wav = fl.render("mix.wav")
bass = fl.render("bass.wav", bars=4, solo="Bass")
stems = fl.render("take.wav", stems="channels", bars=8)
```

From the `run` tool only the file name is used: the WAV lands in the session's
read-only renders folder and the returned path says where ([38](38-python-kernel.md)).

### `fl.scene(detail="summary", channel=None, pattern=None)`, `fl.mixer`, `fl.stock_folder`

Plain objects for model code, since 2026-11-03.
`fl.scene()` is what the `get_scene` tool returns (`SceneSummary`: tempo, inferred key, the playlist and a digest per channel); `fl.scene("notes", channel=, pattern=)` is every note FL has (`SceneNotesResult`), optionally of one channel (a name or index) and one pattern.
`fl.mixer` lists FL's mixer tracks now (`MixerTrack`: index, name, volume, pan, muted, solo; 0 is the master).
`fl.stock_folder` is FL's `Data/Patches` folder, which run code may read but not change: a `Sound`'s `path` from `fl.sounds` is relative to it, so a sample can be loaded and listened to before it is chosen.

```python
kick = fl.sounds("808 kick", kind="sample")[0]
samples, rate = fl.read_wav(fl.stock_folder / kick.path)
```

### `fl.read_wav(path: str | Path) -> tuple[np.ndarray, int]`

**Availability:** local file read. Loads a PCM WAV as `(samples, sample_rate)`, and FL's stock samples too: FL 2026 ships them as Ogg Vorbis inside a WAV (format tag `0x674F`), which Python's `wave`, scipy and libsndfile all refuse, so `read_wav` decodes the Ogg stream inside (since 2026-11-03).
`samples` is a float64 NumPy array shaped `(frames, channels)`, normalized to
roughly `[-1, 1]`; `sample_rate` is Hz.

This is how the model listens: there is no canned analysis.
It reads the samples and computes or plots what it wants with numpy, scipy or matplotlib, and shows a figure by ending the cell with it ([38](38-python-kernel.md#images)).
Until 2026-11-03 the SDK also had `fl.analyze` (tempo, key, LUFS, true peak, spectral balance) and `fl.spectrogram` (a fixed picture), and the `analyze` tool returned both; the owner removed them so the model builds the analysis it needs.

```python
import matplotlib.pyplot as plt
import numpy as np

wav = fl.render("mix.wav")
samples, rate = fl.read_wav(wav)
plt.specgram(samples.mean(axis=1), Fs=rate, NFFT=4096, cmap="magma")
for start in np.arange(0, wav.span.seconds, 240 / wav.span.tempo_bpm):
    plt.axvline(start, color="white", lw=0.5)  # bar lines, 4 beats a bar
# a figure still open when the cell ends is shown to the model
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

### `fl.route(channel: str | int, mixer_track: int) -> ChannelRoute`

**Availability:** live-only. Plays the channel (index, exact name, or a unique part of the name, as `fl.track`) through `mixer_track`, where 0 is the master.
A mixer track FL does not have is refused before anything is written.
Returns `ChannelRoute(channel, mixer_track)` read back from `channels.getTargetFxTrack`; a route FL does not keep raises.

```python
fl.route("FLEX Bass", 3)
```

### `fl.send(src_track: int, dst_track: int, level: float | None = None, on: bool = True) -> MixerSend`

**Availability:** live-only. Turns the send from mixer track `src_track` to `dst_track` on, or off with `on=False`, then applies the routing change.
`level` is normalized 0 to 1 (FL's default send is 0.8); `None` keeps the send's level.
Returns `MixerSend(source, target, on, level)` read back from FL; a send that is off has `level=None`, since FL keeps no level for it (it answers "Index out of range"), and `level` with `on=False` is refused.
If FL takes the send but refuses its level, the error says the send landed.

```python
fl.send(3, 5, level=0.5)   # mixer track 3 also feeds mixer track 5
fl.send(3, 5, on=False)
```

### `fl.presets(channel: str | int) -> list[str]`

**Availability:** live-only. The names of the channel plugin's presets, in FL's order; a preset's index is its place in this list.
Every name is one controller round trip.
A sampler channel is not a plugin and raises `invalid_argument`; `fl.load_sample` changes its sound, and `fl.load_preset` with an instrument preset makes it a plugin channel.

### `fl.set_preset(channel: str | int, preset: int | str) -> ChannelPreset`

**Availability:** live-only. Switches the channel's plugin to a preset by index, by name (any case), or by a unique part of a name.
FL has no getter for the current preset, but stepping renames the channel to the preset's name.
So `set_preset` reads where the channel's name says it is, steps there with `plugins.nextPreset` or `prevPreset`, and confirms by the name it ends on.
A channel whose name shows no preset, or a name two presets share, takes one step first to learn its place.
Returns `ChannelPreset(channel, index, name)`, where `name` is the channel's new name.
The preset replaces the plugin's sound, including earlier `set_param` changes.
If it lands on another preset (a channel renamed by hand to a preset's name), it raises; calling again steps from the preset FL now shows.

```python
names = fl.presets("FLEX Bass")
fl.set_preset("FLEX Bass", "Pure Feather")
```

### `fl.sounds(query: str = "", kind: "preset" | "sample" | None = None, plugin: str | None = None, limit: int = 20) -> list[Sound]`

**Availability:** local file search of this FL install's stock content (`Data/Patches`), read by the engine, not the `run` kernel.
Returns sounds whose path holds every word of `query` (any case), in FL's folder order, up to `limit` (at most 200).
`kind` keeps only presets or only samples; `plugin` keeps that plugin's presets.
A missing stock folder raises `unknown_target` rather than returning an empty list.
`fl.load_sample` and `fl.load_preset` take a found `Sound` or its `path`.

```python
kicks = fl.sounds("808 kick", kind="sample")
```

### `fl.load_sample(channel: str | int, sample: Sound | str) -> ChannelSound`

**Availability:** live-only, through the FL MCP Bridge. Loads a stock sample (a `Sound` from `fl.sounds(kind="sample")`, or its `path`) into a sampler channel.
FL renames the channel after the sample (`"808 Cowbell"`), numbering it when another channel already has that name (`"808 Kick #2"`).
Returns `ChannelSound(channel, kind, sound, name, plugin=None)` read back from FL; a channel that does not show the sample raises.
A plugin channel is refused before anything changes: FL only renames it. Give it an instrument preset with `fl.load_preset` instead.

### `fl.load_preset(channel: str | int, preset: Sound | str) -> ChannelSound`

**Availability:** live-only, through the FL MCP Bridge. Loads a stock preset (a `Sound` from `fl.sounds(kind="preset")`, or its `path`) into a channel.
What it does depends on the preset's folder:

- An instrument preset (`Plugin presets/Generators/<plugin>/...`) puts that plugin on the channel, replacing its instrument, a sampler included, and FL names the channel after the preset.
  This is how to change a channel's instrument; the channel keeps its mixer track.
  Returns the channel's new `name` and `plugin`; a channel that does not then play that plugin under the preset's name raises.
- An effect preset (`Plugin presets/Effects/<plugin>/...`) leaves the instrument alone and adds that effect to the next free slot of the mixer track the channel plays through.
  `effect` is `MixerEffect(mixer_track, slot, plugin)`, read from the mixer track's slots before and after; a full mixer track is refused first.
  Each load adds another effect, so loading the same effect preset twice gives two effects.

VST presets (`Plugin presets/VST/...`) are refused: what they load as was not verified.
FL loads into every selected channel, so the engine selects exactly this channel first and confirms that no other one stays selected.
A trial FL marks some plugins "TRIAL VERSION" in their window.

```python
osc = fl.sounds(kind="preset", plugin="3x Osc")[0]
fl.load_preset("808 Snare", osc)          # channel 3 now plays 3x Osc
room = fl.sounds("drum room", kind="preset", plugin="Fruity Reeverb 2")[0]
fl.load_preset(3, room).effect            # MixerEffect(mixer_track=4, slot=0, ...)
```

### `fl.undo() -> None`

**Availability:** live-only. Invokes FL's undo command. The session's note
record does not follow it, so write the track again to set its notes for certain.
FL keeps a solo or mute render's mute changes in its undo history, so an undo right after one would mute a channel again (box run 2026-11-03); since then an undo that changed a channel's mute sets it back and raises `fl_api_error` saying nothing else was undone.

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
record and returns the saved-file readback of the slot it filled. Current evidence only
supports `fl.add_effect(0, "Fruity Parametric EQ 2")`: Master's first slot (FL's slot 1), when empty.
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
| `Note` | Positional or keyword, in this order: `Note(60, 0, 1, 0.8)` is `Note(key=60, start=0, length=1, velocity=0.8)`. `key: int` (MIDI 0–127; note names such as `"C4"` are accepted on construction), `start: float` (beats), `length: float` (beats, >0), `velocity: float` (0–1, default .8), `pan: float` (0–1, default .5), `color: int` (0–15, default 0) |
| `NoteWriteMode` | Literal `"merge"` or `"replace"` |
| `AutomationPoint` | `position: float` (beats, >=0), `value: float` (normalized, -0.001–1.001), `tension: float` (-1–1, default 0) |
| `ChannelLevels` | `channel: int`, `volume: float` (0–1), `pan: float` (-1–1), `pitch_semitones: int` (-48–48) |
| `Effect` | `name: str`, `chunk: bytes` (opaque FL-authored plugin state) |
| `AudioAnalysis` | `bpm: float | None` (FL's tempo for a session render), `key: str | None`, `integrated_lufs: float`, `true_peak_dbfs: float`, `spectral_balance: SpectralBalance` (`low_dbfs`, `mid_dbfs`, `high_dbfs`); decibels in tenths |
| `ChannelRoute` | `channel: int`, `mixer_track: int` (0 is the master) |
| `MixerSend` | `source: int`, `target: int`, `on: bool`, `level: float | None` (normalized, FL's default 0.8; `None` when the send is off) |
| `ChannelPreset` | `channel: int`, `index: int` (place in `fl.presets`), `name: str` (the channel's name now) |
| `Sound` | `kind: "preset" | "sample"`, `name: str`, `plugin: str | None` (a preset's plugin), `path: str` (relative to FL's `Data/Patches`, `/`-separated) |
| `ChannelSound` | `channel: int`, `kind: "preset" | "sample"`, `sound: str` (the loaded `Sound.path`), `name: str` (the channel's name now), `plugin: str | None` (its instrument plugin; `None` for a sampler), `effect: MixerEffect | None` (what an effect preset added) |
| `MixerEffect` | `mixer_track: int`, `slot: int` (0-9; FL shows slot 1-10), `plugin: str` (FL's name for it) |
| `PluginParam` | `index: int`, `name: str`, `value: float` (normalized 0–1), `display: str`, `kind: "continuous" | "stepped" | "enum" | "bool"`, `options: list[str] | None`, `minimum`, `maximum`, `step`, `unit` |
