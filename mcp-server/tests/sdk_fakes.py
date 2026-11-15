"""Lane K test seam: a fake FLSession wired to fakes, per docs/21.

``sdk.connect(fake)`` is the deliberate injection point. FL IO is faked; the
Store is REAL (on tmp_path) so the write-through path - record_write, deltas,
the action log - is the shipped code, not a mock of it.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from session_fakes import RENDER_SPAN

if TYPE_CHECKING:
    from fl_mcp.kernel.host import KernelHost

from fl_mcp.schemas import (
    Channel,
    ChannelCheck,
    ChannelLevels,
    CheckResult,
    ControlResponse,
    MixerEffect,
    MixerSend,
    MixerTrack,
    Note,
    NoteWriteMode,
    PluginParam,
    RenderReceipt,
    SessionState,
    StemsReceipt,
    Target,
    WriteResult,
)
from fl_mcp.session import FLSession
from fl_mcp.session.link import ControlLink
from fl_mcp.store import Store


class FakeControl:
    """Minimal control plane: records ops, flips transport state, fails on demand."""

    def __init__(self) -> None:
        self.playing = False
        self.ops: list[str] = []
        self.fail_with: str | None = None

    def send_request(self, op, args=None, kwargs=None, timeout=2.0) -> ControlResponse:
        self.ops.append(op)
        if self.fail_with is not None:
            return ControlResponse(id=len(self.ops), ok=False, err=self.fail_with)
        if op == "transport.start":
            self.playing = True
        elif op == "transport.stop":
            self.playing = False
        return ControlResponse(id=len(self.ops), ok=True)


class FakeSession(FLSession):
    """Duck-typed FLSession whose backend is the in-memory store itself.

    Every write really lands in the injected (real) Store and every read comes
    back from it, so receipts carry genuine readback counts and deltas.
    ``fail_next_write`` simulates a backend rejection: the exception raises
    BEFORE anything is applied, exactly like session.write_notes promises.
    """

    def __init__(self, *, store: Store, channels=(), control=None, tempo: float | None = 120.0):
        self.link = ControlLink(control, factory=lambda: control, sleep=lambda seconds: None)
        self.store = store
        self.project = None
        self._tempo = tempo
        self.fail_next_write: Exception | None = None
        self._channels = list(channels)
        self._next_id = 0
        self.level_calls: list[tuple[int, float | None, float | None, int | None]] = []
        self.renders: list[dict] = []
        # One effect on the mixer: a limiter in slot 0 of track 17.
        self._effects: dict[int, list[MixerEffect]] = {17: [MixerEffect(mixer_track=17, slot=0, plugin="Fruity Limiter")]}

    def mixer_tracks(self) -> list[MixerTrack]:
        return [MixerTrack(index=i, name=f"Insert {i}", effects=list(fx)) for i, fx in sorted(self._effects.items())]

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteResult:
        if self.fail_next_write is not None:
            exc, self.fail_next_write = self.fail_next_write, None
            raise exc
        self._next_id += 1
        before = self.store.notes_at(target) or []
        after = [*before, *notes] if mode == "merge" else list(notes)
        self.store.record_write(target, before, after, mode, "project-write", self._next_id)
        name = next((c.name for c in self._channels if c.index == target.channel), f"Channel {target.channel}")
        return WriteResult(wrote=len(notes), track=name, pattern=target.pattern, mode=mode, total=len(after))

    def clear_notes(self, target: Target) -> WriteResult:
        return self.write_notes(target, [], "replace")

    def read_notes(self, target: Target) -> list[Note]:
        return self.store.notes_at(target) or []

    def tempo(self) -> float | None:
        return self._tempo

    def set_tempo(self, bpm: float) -> float:
        # FL stores tempo as u32 BPM x 1000; the readback is that quantized
        # value, not the caller's echo.
        self._tempo = round(bpm * 1000) / 1000
        return self._tempo

    def render_wav(
        self, output: Path, start_bar: int = 1, bars: int | None = None, solo: tuple = (), mute: tuple = ()
    ) -> RenderReceipt:
        self.renders.append({"output": output, "start_bar": start_bar, "bars": bars, "solo": solo, "mute": mute})
        output.write_bytes(b"RIFF....WAVE")
        span = RENDER_SPAN.model_copy(update={"start_bar": start_bar})
        return RenderReceipt(path=output, span=span, solo=list(solo), mute=list(mute))

    def render_stems(
        self, output: Path, by: str, start_bar: int = 1, bars: int | None = None, channels: tuple[int, ...] = ()
    ) -> StemsReceipt:
        """Every channel, or the ones named; which channels have notes is the real session's
        to decide (test_render_span), so this fake records all and skips none."""
        chosen = channels or tuple(c.index for c in self._channels)
        return StemsReceipt(
            stems=[
                self.render_wav(output.with_name(f"{output.stem}-{i}.wav"), start_bar, bars, solo=(i,))
                for i in chosen
            ]
        )

    def export_offline(self, output: Path) -> Path:
        return self.render_wav(output).path

    def save_project(self) -> int | None:
        if self.control is None:
            from fl_mcp import errors

            raise errors.control_plane_unavailable("ctl.save", 0.0)
        return self.control.send_request("ctl.save").ret

    def set_channel_levels(
        self,
        channel: int,
        *,
        volume: float | None = None,
        pan: float | None = None,
        pitch_semitones: int | None = None,
    ) -> ChannelLevels:
        self.level_calls.append((channel, volume, pan, pitch_semitones))
        return ChannelLevels(
            channel=channel,
            volume=0.8 if volume is None else volume,
            pan=0.0 if pan is None else pan,
            pitch_semitones=0 if pitch_semitones is None else pitch_semitones,
        )

    def plugin_params(
        self, channel: int, *, query: str | None = None, limit: int = 40
    ) -> list[PluginParam]:
        params = [
            PluginParam(index=0, name="Cutoff", value=0.25, display="0.25", kind="continuous"),
            PluginParam(index=1, name="Resonance", value=0.5, display="0.5", kind="continuous"),
        ]
        if query is not None:
            params = [param for param in params if query.lower() in param.name.lower()]
        return params[:limit]

    def set_plugin_param(self, channel: int, param: int | str, value: float) -> PluginParam:
        name = "Cutoff" if param == 0 else str(param)
        return PluginParam(
            index=0 if isinstance(param, str) else param,
            name=name,
            value=value,
            display=f"{value:g}",
            kind="continuous",
        )

    def effect_params(
        self, mixer_track: int, slot: int, *, query: str | None = None, limit: int = 40
    ) -> list[PluginParam]:
        return self.plugin_params(mixer_track, query=query, limit=limit)

    def set_effect_param(self, mixer_track: int, slot: int, param: int | str, value: float) -> PluginParam:
        return self.set_plugin_param(mixer_track, param, value)

    @contextlib.contextmanager
    def _write_guard(self, action: str) -> Iterator[None]:
        yield  # one caller at a time in these tests; the real guard is tested on FLSession

    def snapshot(self) -> SessionState:
        return SessionState(tempo_bpm=self._tempo, channels=list(self._channels))

    def check(self, start_bar: int = 1, bars: int = 2) -> CheckResult:
        span = f"bars {start_bar}-{start_bar + bars - 1}"
        heard = ChannelCheck(channel=0, name="Kick", sounded=True, peak_dbfs=-6.0, mixer_track=1)
        return CheckResult(
            start_bar=start_bar,
            bars=bars,
            seconds=bars * 2.0,
            master_peak_dbfs=-6.0,
            channels=[heard],
            summary=f"1 of 1 channels sounded in {span}.",
        )


DEFAULT_CHANNELS = (
    Channel(index=0, name="Kick"),
    Channel(index=1, name="Bass"),
    Channel(index=2, name="Lead"),
)


def started_ahead(host: KernelHost, seconds: float = 60.0) -> KernelHost:
    """``host`` with its kernel already started, so a test's run budget times the cell
    rather than the start (a start inside a run counts against the run's budget)."""
    host.start()
    deadline = time.monotonic() + seconds
    while host._kernel is None:
        assert time.monotonic() < deadline, "the kernel did not start"
        time.sleep(0.01)
    return host


# The agent template's channels and their mixer tracks (scripts/build_session_template.py).
STUDIO_CHANNELS = (
    ("Kick", 4), ("Snare", 6), ("Clap", 7), ("Hat", 8), ("Open Hat", 5), ("Perc", 24), ("Crash", 9),
    ("Cowbell", 10), ("808", 11), ("Bass", 25), ("Synth Bass", 12), ("Piano", 26), ("E-Piano", 27),
    ("Strings", 16), ("Pad", 17), ("Choir", 19), ("Brass", 28), ("Woodwinds", 29), ("Sax", 30),
    ("Guitar", 31), ("Lead", 14), ("Pluck", 15), ("Synth 1", 32), ("Synth 2", 33),
    ("Sampler 1", 20), ("Sampler 2", 21), ("Sampler 3", 22), ("Audition", 34),
)
# The template's send returns (scripts/build_session_template.py), which the mixing guide finds by name.
STUDIO_SENDS = {113: "Reverb Send", 114: "Delay Send", 116: "Drum Bus"}
# The effects the template puts on its mixer tracks (build_session_template.py EFFECTS), by slot.
STUDIO_EFFECTS = {
    11: ("Fruity Fast Dist", "Fruity Soft Clipper"),
    25: ("Fruity Parametric EQ 2",),
    113: ("Fruity Reeverb 2",),
    114: ("Fruity Delay Bank",),
    116: ("Fruity Parametric EQ 2", "Fruity Limiter"),
}
STUDIO_RATE = 44_100


def studio_wav(path: Path, seconds: float, tempo: float = 120.0) -> Path:
    """A real stereo WAV: a kick-like pulse on every beat over quiet noise, so loudness,
    peak and spectrum code reads plausible numbers."""
    import numpy as np
    import soundfile

    frames = max(1, int(seconds * STUDIO_RATE))
    t = np.arange(frames) / STUDIO_RATE
    since_beat = t % (60 / tempo)
    pulse = np.sin(2 * np.pi * 55 * since_beat) * np.exp(-since_beat * 12)
    noise = np.random.default_rng(len(path.name)).normal(0, 0.02, frames)
    mono = 0.5 * pulse + noise
    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(path, np.stack([mono, mono], axis=1), STUDIO_RATE, subtype="PCM_16")  # as FL renders
    return path


class StudioFake(FakeSession):
    """The whole session surface the SDK calls, over the agent template's channels:
    what a guide's worked example runs against in CI (fl_mcp/guides). Writes succeed
    and return what was asked; renders, auditions and library sounds are real WAVs.
    Like FL, loading a sound or a preset renames the channel after it."""

    def __init__(self, *, store: Store, library: Path) -> None:
        from fl_mcp.schemas import PatternInfo, PlaylistClip

        channels = [Channel(index=i, name=name, mixer_track=track) for i, (name, track) in enumerate(STUDIO_CHANNELS)]
        super().__init__(store=store, channels=channels)
        self.library = library
        self._volumes = {0: 0.8, **{c.mixer_track: 0.8 for c in channels}, **dict.fromkeys(STUDIO_SENDS, 0.8)}
        self._effects: dict[int, list] = {
            track: [MixerEffect(mixer_track=track, slot=slot, plugin=plugin) for slot, plugin in enumerate(plugins)]
            for track, plugins in STUDIO_EFFECTS.items()
        }
        self._sends: dict[tuple[int, int], MixerSend] = {}
        self._patterns = [PatternInfo(number=1, name="Pattern 1", beats=16.0)]
        self._clips = [PlaylistClip(track=1, start=0.0, length=16.0, pattern=1)]
        self._written: dict[tuple[int, int], Target] = {}  # Target is not hashable

    # -- reads
    def channel_names(self) -> list[Channel]:
        return list(self._channels)

    def live_channels(self) -> list[Channel]:
        return list(self._channels)

    def mixer_tracks(self):
        names = {0: "Master", **{c.mixer_track: c.name for c in self._channels}, **STUDIO_SENDS}
        return [
            MixerTrack(
                index=index, name=name, volume=self._volumes.get(index, 0.8),
                effects=list(self._effects.get(index, [])),
                sends=[send for (src, _), send in self._sends.items() if src == index],
            )
            for index, name in sorted(names.items())
        ]

    def patterns(self):
        return list(self._patterns)

    def playlist(self):
        return list(self._clips)

    def scene_summary(self):
        from fl_mcp.contract import SceneSummary

        notes = sum(len(self.read_notes(t)) for t in self._written.values())
        return SceneSummary(
            tempo=self._tempo, key=None, note_count=notes, playlist={"clips": len(self._clips)},
            channels=[{"index": c.index, "name": c.name} for c in self._channels],
            summary=f"{len(self._channels)} channels, {notes} notes.",
        )

    def scene_notes(self, channel=None, pattern=None):
        from fl_mcp.contract import SceneNote, SceneNotesResult
        from fl_mcp.pitch import key_name

        return SceneNotesResult(notes=[
            SceneNote(channel=t.channel, pattern=t.pattern, key=key_name(n.key), midi=n.key,
                      start=n.start, length=n.length, velocity=n.velocity)
            for _, t in sorted(self._written.items())
            if channel in (None, t.channel) and pattern in (None, t.pattern)
            for n in self.read_notes(t)
        ])

    # -- notes
    def write_notes(self, target, notes, mode):
        self._written[(target.channel, target.pattern)] = target
        return super().write_notes(target, notes, mode)

    # -- song
    def _pattern(self, ref):
        found = next((p for p in self._patterns if ref in (p.number, p.name)), None)
        if found is None:
            from fl_mcp import errors

            raise errors.invalid_argument("fl.place", f"no pattern {ref!r}")
        return found

    def new_pattern(self, name, copy_of=None):
        from fl_mcp.schemas import PatternInfo

        beats = 0.0 if copy_of is None else self._pattern(copy_of).beats
        made = PatternInfo(number=len(self._patterns) + 1, name=name, beats=beats)
        self._patterns.append(made)
        return made

    def rename_pattern(self, pattern, name):
        found = self._pattern(pattern)
        renamed = found.model_copy(update={"name": name})
        self._patterns[self._patterns.index(found)] = renamed
        return renamed

    def place(self, pattern, bars):
        from fl_mcp import errors
        from fl_mcp.schemas import PlacedClip, PlaylistClip

        found = self._pattern(pattern)
        first = min((c.start for c in self._clips if c.pattern == found.number), default=None)
        if first is not None and min(bars) > first / 4 + 1:
            raise errors.invalid_argument("fl.place", "FL adds a pattern's clip only before its first clip")
        beats = found.beats or 16.0
        self._clips += [PlaylistClip(track=1, start=(b - 1) * 4.0, length=beats, pattern=found.number) for b in bars]
        return [PlacedClip(pattern=found.number, name=found.name, bar=b, beats=beats, track=1) for b in bars]

    def automate(self, points, bar, param, channel, mixer_track, slot, pattern):
        from fl_mcp.schemas import AutomationTake, PlaylistClip

        target = f"mixer track {mixer_track} {param}" if channel is None else f"channel {channel} {param}"
        made = self._pattern(pattern) if pattern is not None else self.new_pattern(target)
        beats = max(points[-1].position, 1.0)
        self._clips.append(PlaylistClip(track=2, start=(bar - 1) * 4.0, length=beats, pattern=made.number))
        return AutomationTake(target=target, pattern=made, bar=bar, beats=beats, points=points, max_error=0.01)

    # -- mix
    def route_channel(self, channel, mixer_track):
        from fl_mcp.schemas import ChannelRoute

        self._rename(channel, mixer_track=mixer_track)
        return ChannelRoute(channel=channel, mixer_track=mixer_track)

    def set_send(self, source, target, *, on=True, level=None):
        send = MixerSend(source=source, target=target, on=on, level=(0.8 if level is None else level) if on else None)
        self._sends[(source, target)] = send
        return send

    def presets(self, channel):
        return ["Default", "Warm", "Bright", "Wide"]

    def set_preset(self, channel, preset):
        from fl_mcp.schemas import ChannelPreset

        names = self.presets(channel)
        index = preset if isinstance(preset, int) else next(i for i, n in enumerate(names) if preset.lower() in n.lower())
        self._rename(channel, name=names[index])
        return ChannelPreset(channel=channel, index=index, name=names[index])

    # -- sounds
    def sounds(self, query="", *, like_path=None, like_audio=None, kind=None, plugin=None, limit=20):
        from fl_mcp.schemas import Sound

        kind = kind or "sample"
        words = "-".join(query.split()[:3]) or "sound"
        return [
            Sound(
                kind=kind, name=f"{words} {i + 1}", plugin=plugin or ("FLEX" if kind == "preset" else None),
                path=(f"Plugin presets/Generators/FLEX/{words}-{i + 1}.fst" if kind == "preset"
                      else f"CC0/Fake/{words}-{i + 1}.wav"),
                score=0.9 - 0.1 * i, seconds=1.0 + i, pitch=None, decay=0.2 + 0.1 * i,
                brightness=1500.0 + 300 * i, low=0.4 - 0.05 * i, high=0.05 + 0.02 * i,
            )
            for i in range(limit)
        ]

    def sound_file(self, sound: str) -> Path:
        return studio_wav(self.library / Path(sound).with_suffix(".wav").name, 1.0, self._tempo or 120.0)

    def audition(self, preset, output, notes=None):
        from fl_mcp.schemas import Audition, ChannelSound

        audition = next(c for c in self._channels if c.name == "Audition")
        return Audition(
            sound=ChannelSound(channel=audition.index, kind="preset", sound=preset, name="Audition", plugin="FLEX"),
            render=self.render_wav(output, bars=2, solo=(audition.index,)),
        )

    def load_sound(self, channel, sound, kind):
        from fl_mcp.schemas import ChannelSound

        name = Path(sound).stem
        if kind == "preset" and "/Effects/" in sound:
            track = self._channels[channel].mixer_track
            slots = self._effects.setdefault(track, [])
            effect = MixerEffect(mixer_track=track, slot=len(slots), plugin=Path(sound).parent.name)
            slots.append(effect)
            return ChannelSound(channel=channel, kind=kind, sound=sound, name=self._channels[channel].name,
                                effect=effect)
        self._rename(channel, name=name)
        return ChannelSound(channel=channel, kind=kind, sound=sound, name=name,
                            plugin="FLEX" if kind == "preset" else None)

    def load_audio(self, channel, name, audio):
        from fl_mcp.schemas import ChannelSound

        self._rename(channel, name=Path(name).stem)
        return ChannelSound(channel=channel, kind="sample", sound=name, name=Path(name).stem)

    def _rename(self, channel, **update):
        self._channels[channel] = self._channels[channel].model_copy(update=update)

    # -- hear
    def render_wav(self, output, start_bar=1, bars=None, solo=(), mute=()):
        from fl_mcp.schemas import RenderSpan

        if bars is None and not solo and not mute and not any(map(self.read_notes, self._written.values())):
            from fl_mcp import errors

            # As FL: a whole-song render that records silence is refused (session/controls.py).
            raise errors.ToolError("render_failed", "The render recorded silence.", "Write notes, then render again.")
        tempo = self._tempo or 120.0
        beats = 4.0 * (bars or 8)
        seconds = beats * 60 / tempo
        self.renders.append({"output": output, "start_bar": start_bar, "bars": bars, "solo": solo, "mute": mute})
        studio_wav(Path(output), seconds, tempo)
        span = RenderSpan(source="song", start_bar=start_bar, beats=beats, tempo_bpm=tempo, seconds=seconds)
        return RenderReceipt(path=Path(output), span=span, solo=list(solo), mute=list(mute))

    # -- project
    def save_project(self):
        return None

    def undo(self) -> None:
        return None
