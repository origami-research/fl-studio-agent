"""The pythonic surface the model drives - a live FL session as an object.

LANE K implements this module (docs/21). Plain Python semantics, per the
owner's call (2026-08): every method APPLIES IMMEDIATELY and either returns
the confirmed result or raises with the reason. No queues, no ``flush()``, no
``cell()``, no partial states - one call is the atomic unit, and an error
reads like Python: a traceback pointing at the failing line.

Helpers: none, by design. The methods ARE the API; anything reusable lives in
session/flp/store. A private function appearing here is a design smell.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from fl_mcp import errors
from fl_mcp.audio import read_wav
from fl_mcp.contract import SceneNotesResult, SceneSummary

from .flp import Effect
from .schemas import (
    AutomationPoint,
    AutomationResult,
    Channel,
    ChannelLevels,
    ChannelPreset,
    ChannelRoute,
    ChannelSound,
    CheckResult,
    MixerSend,
    MixerTrack,
    Note,
    NoteWriteMode,
    PatternInfo,
    PlaylistClip,
    PluginParam,
    RenderReceipt,
    RenderSpan,
    Sound,
    SoundKind,
    StemsReceipt,
    Target,
    WriteResult,
)
from .session import FLSession, create_session
from .sounds import SEARCH_LIMIT_MAX

SdkChannel = Annotated[int, Field(strict=True, ge=0)]
SdkPattern = Annotated[int, Field(strict=True, ge=1)]
SdkLimit = Annotated[int, Field(strict=True, ge=0)]
NormalizedValue = Annotated[float, Field(strict=True, ge=0, le=1)]

NoteInput = Note | dict[str, object]
AutomationPointInput = AutomationPoint | dict[str, object]


class _TrackArgs(BaseModel):
    name_or_index: str | SdkChannel
    pattern: SdkPattern = 1


class _TargetArgs(BaseModel):
    channel: SdkChannel
    pattern: SdkPattern = 1


class _ParamsArgs(BaseModel):
    query: str | None = None
    limit: SdkLimit = 40


class _ParamSetArgs(BaseModel):
    param: str | SdkChannel
    # A normalized float is validated here; a str (display/option value) is
    # passed through and validated against the captured plugin model downstream.
    value: NormalizedValue | Annotated[str, Field(strict=True)]


class _ChannelLevelsArgs(BaseModel):
    volume: NormalizedValue | None = None
    pan: Annotated[float, Field(strict=True, ge=-1, le=1)] | None = None
    pitch: Annotated[int, Field(strict=True, ge=-48, le=48)] | None = None


class _AddEffectArgs(BaseModel):
    insert_index: SdkChannel
    plugin_name: Annotated[str, Field(strict=True, min_length=1)]


class _RouteArgs(BaseModel):
    mixer_track: SdkChannel


class _SendArgs(BaseModel):
    src_track: SdkChannel
    dst_track: SdkChannel
    level: NormalizedValue | None = None
    on: Annotated[bool, Field(strict=True)] = True


class _SetPresetArgs(BaseModel):
    preset: SdkChannel | Annotated[str, Field(strict=True, min_length=1)]


class _SoundsArgs(BaseModel):
    query: Annotated[str, Field(strict=True)] = ""
    kind: SoundKind | None = None
    plugin: Annotated[str, Field(strict=True)] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=SEARCH_LIMIT_MAX)] = 20


class _RenderArgs(BaseModel):
    bars: Annotated[int, Field(strict=True, ge=1)] | None = None
    start_bar: Annotated[int, Field(strict=True, ge=1)] = 1
    stems: (
        Literal["channels", "mixer"]
        | Annotated[list[Annotated[str, Field(strict=True)] | SdkChannel], Field(min_length=1)]
        | None
    ) = None


class _LoadArgs(BaseModel):
    """A stock sound to load: a Sound from fl.sounds() of the right kind, or its path."""

    kind: SoundKind
    sound: Sound | Annotated[str, Field(strict=True, min_length=1)]

    @model_validator(mode="after")
    def _same_kind(self) -> _LoadArgs:
        if isinstance(self.sound, Sound) and self.sound.kind != self.kind:
            raise ValueError(f"{self.sound.name!r} is a {self.sound.kind}, not a {self.kind}")
        return self

    @property
    def path(self) -> str:
        return self.sound.path if isinstance(self.sound, Sound) else self.sound


_NOTES = TypeAdapter(list[Note])
_AUTOMATION_POINTS = TypeAdapter(list[AutomationPoint])
_WRITE_MODE = TypeAdapter(NoteWriteMode)


class Render(type(Path())):
    """A rendered WAV: its path (open it, read it with fl.read_wav, pass it to soundfile)
    that also says what played: ``span`` (start_bar, beats, tempo_bpm, seconds) and the
    channels ``solo``-ed and ``mute``-d for it."""

    span: RenderSpan
    solo: tuple[int, ...]
    mute: tuple[int, ...]

    @classmethod
    def of(cls, receipt: RenderReceipt) -> Render:
        render = cls(receipt.path)
        render.span, render.solo, render.mute = receipt.span, tuple(receipt.solo), tuple(receipt.mute)
        return render

    def __repr__(self) -> str:
        span = getattr(self, "span", None)
        if span is None:  # a path derived from a render (.parent, / "x") is a plain path
            return super().__repr__()
        heard = "".join(f", {k}={v!r}" for k, v in (("solo", self.solo), ("mute", self.mute)) if v)
        return f"Render({str(self)!r}, span={span!r}{heard})"


class Stems(list[Render]):
    """The stems a render recorded, one Render each, and ``skipped``: the channels it
    left out because they have no notes in the bars it played."""

    skipped: tuple[int, ...]

    @classmethod
    def of(cls, receipt: StemsReceipt) -> Stems:
        stems = cls(Render.of(r) for r in receipt.stems)
        stems.skipped = tuple(receipt.skipped)
        return stems

    def __repr__(self) -> str:
        return f"Stems({list.__repr__(self)}, skipped={getattr(self, 'skipped', ())!r})"


class ChannelNotFound(ValueError):
    """Raised with the real channel names so the caller can pick one."""


def connect(session: FLSession | None = None) -> FL:
    """The entrypoint. Builds the default session (``create_session()``) when
    none is injected; tests inject one wired to fakes."""
    return FL(session if session is not None else create_session())


class FL:
    """The live FL Studio session. Edits apply immediately; reads return what FL has now."""

    def __init__(self, session: FLSession) -> None:
        self._session = session

    @property
    def channels(self) -> list[Channel]:
        """The channels in FL now, each with what the project's notes say it is for (``about``)."""
        return self._session.live_channels()

    @property
    def mixer(self) -> list[MixerTrack]:
        """FL's mixer tracks in use now (0 is the master): index, name, volume, pan, muted,
        solo, effects (slot and plugin) and sends (target and level). A track is in use
        when a channel plays into it, it holds an effect, it was renamed, or a track in
        use sends to it."""
        return self._session.mixer_tracks()

    @property
    def playlist(self) -> list[PlaylistClip]:
        """The playlist's clips in the song FL opened: track, start and length in beats,
        and the pattern (or audio channel) each plays. The SDK cannot change the playlist."""
        return self._session.playlist()

    @property
    def patterns(self) -> list[PatternInfo]:
        """FL's patterns now: number, name and length in beats."""
        return self._session.patterns()

    @property
    def stock_folder(self) -> Path:
        """FL's stock presets and samples (its Data/Patches folder), which run code may
        read but not change; a Sound's path from fl.sounds is relative to it."""
        return self._session.stock_folder()

    def scene(
        self, detail: Literal["summary", "notes"] = "summary", channel: str | int | None = None, pattern: int | None = None
    ) -> SceneSummary | SceneNotesResult:
        """A digest of the song, as objects: detail="summary" for tempo, key, the
        playlist and a digest per channel; "notes" for every note FL has (by channel and
        pattern), optionally of one channel and one pattern."""
        if detail == "summary":
            return self._session.scene_summary()
        if detail != "notes":
            raise errors.invalid_argument("fl.scene", f'detail must be "summary" or "notes", got {detail!r}')
        return self._session.scene_notes(None if channel is None else self.track(channel).channel, pattern)

    @property
    def tempo(self) -> float:
        """FL's tempo now, in BPM."""
        tempo = self._session.tempo()
        if tempo is None:
            raise errors.control_plane_unavailable("mixer.getCurrentTempo", 0.0)
        return tempo

    def track(self, name_or_index: str | int, pattern: int = 1) -> Track:
        """A channel's piano roll in ``pattern``: by index, exact name, or a unique
        part of the name, as FL names it now. Raises ChannelNotFound listing the
        channels otherwise.
        """
        # THE place channel names resolve - Target is ints-only past this
        # boundary. The names are FL's live ones whenever FL is connected
        # (session/state.py channel_names), so a channel FL just renamed resolves
        # by its new name. A reload settles before returning, so no live read
        # lands in FL's boot window (dogfood A12).
        try:
            args = _TrackArgs(name_or_index=name_or_index, pattern=pattern)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.track", str(exc)) from None
        channels = self._session.channel_names()
        listing = ", ".join(f"{c.index}: {c.name}" for c in channels) or "(none)"
        if isinstance(args.name_or_index, int):
            if any(c.index == args.name_or_index for c in channels):
                return Track(self, args.name_or_index, args.pattern)
            raise ChannelNotFound(f"No channel at index {args.name_or_index}. Channels: {listing}")
        exact = [c for c in channels if c.name == args.name_or_index]
        if len(exact) == 1:
            return Track(self, exact[0].index, args.pattern)
        partial = [c for c in channels if args.name_or_index.lower() in c.name.lower()]
        if len(partial) == 1:
            return Track(self, partial[0].index, args.pattern)
        if len(exact) > 1 or len(partial) > 1:
            raise ChannelNotFound(f"{args.name_or_index!r} is ambiguous. Channels: {listing}")
        raise ChannelNotFound(f"No channel matching {args.name_or_index!r}. Channels: {listing}")

    def set_tempo(self, bpm: float) -> float:
        """Set FL's tempo now; returns the tempo FL reports afterwards."""
        return self._session.set_tempo(bpm)

    def add_effect(self, insert_index: int, plugin_name: str) -> tuple[Effect, ...]:
        """Add Fruity Parametric EQ 2 to Master's first slot, when empty (FL's slot 1,
        the one supported placement); returns the effect now in that slot.
        Outside a match only.
        """
        try:
            args = _AddEffectArgs(insert_index=insert_index, plugin_name=plugin_name)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.add_effect", str(exc)) from None
        return self._session.add_effect(args.insert_index, args.plugin_name)

    def route(self, channel: str | int, mixer_track: int) -> ChannelRoute:
        """Play a channel (index, exact name, or a unique part of the name) through
        ``mixer_track``, where 0 is the master. Returns the mixer track FL now routes it to.
        """
        try:
            args = _RouteArgs(mixer_track=mixer_track)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.route", str(exc)) from None
        return self._session.route_channel(self.track(channel).channel, args.mixer_track)

    def send(self, src_track: int, dst_track: int, level: float | None = None, on: bool = True) -> MixerSend:
        """Send mixer track ``src_track`` to mixer track ``dst_track`` (0 is the master),
        or stop sending with ``on=False``. ``level`` is 0..1 (FL's default send is 0.8);
        None keeps the send's level. Returns the send as FL now has it; a send that is
        off has level None.
        """
        try:
            args = _SendArgs(src_track=src_track, dst_track=dst_track, level=level, on=on)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.send", str(exc)) from None
        if args.src_track == args.dst_track:
            raise errors.invalid_argument("fl.send", "a mixer track cannot send to itself")
        if not args.on and args.level is not None:
            raise errors.invalid_argument("fl.send", "a send that is off has no level; pass level only with on=True")
        return self._session.set_send(args.src_track, args.dst_track, on=args.on, level=args.level)

    def presets(self, channel: str | int) -> list[str]:
        """The names of a plugin channel's presets, in FL's order: a preset's index is
        its place in this list. A sampler channel has none and raises."""
        return self._session.presets(self.track(channel).channel)

    def set_preset(self, channel: str | int, preset: int | str) -> ChannelPreset:
        """Switch a plugin channel to a preset from ``fl.presets(channel)``: by index, by
        name (any case), or by a unique part of a name. FL steps through its presets to
        get there and renames the channel to the preset; returns the preset FL now has.
        The preset replaces the plugin's sound, including earlier set_param changes.
        """
        try:
            args = _SetPresetArgs(preset=preset)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.set_preset", str(exc)) from None
        return self._session.set_preset(self.track(channel).channel, args.preset)

    def sounds(
        self, query: str = "", kind: SoundKind | None = None, plugin: str | None = None, limit: int = 20
    ) -> list[Sound]:
        """FL's stock sounds whose path holds every word of ``query`` (any case):
        ``Sound(kind, name, plugin, path)``, where kind is "preset" or "sample".
        ``kind`` keeps one kind; ``plugin`` keeps that plugin's presets (e.g. "FLEX");
        ``limit`` caps the list (at most 200).
        """
        try:
            args = _SoundsArgs(query=query, kind=kind, plugin=plugin, limit=limit)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.sounds", str(exc)) from None
        return self._session.sounds(args.query, kind=args.kind, plugin=args.plugin, limit=args.limit)

    def load_sample(self, channel: str | int, sample: Sound | str) -> ChannelSound:
        """Load a stock sample into a sampler channel: a Sound from
        ``fl.sounds(kind="sample")``, or its path. FL renames the channel to the
        sample; returns the channel's name as FL now reports it. A plugin channel
        is refused (give it an instrument preset instead).
        """
        try:
            args = _LoadArgs(kind="sample", sound=sample)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.load_sample", str(exc)) from None
        return self._session.load_sound(self.track(channel).channel, args.path, "sample")

    def load_preset(self, channel: str | int, preset: Sound | str) -> ChannelSound:
        """Load a stock preset into a channel: a Sound from ``fl.sounds(kind="preset")``,
        or its path. An instrument preset puts its plugin on the channel, replacing
        the instrument (a sampler too), and FL names the channel after the preset:
        the way to change a channel's instrument. An effect preset adds its effect to
        the next free slot of the mixer track the channel plays through (``.effect``).
        Returns the channel's name and instrument as FL now reports them.
        """
        try:
            args = _LoadArgs(kind="preset", sound=preset)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.load_preset", str(exc)) from None
        return self._session.load_sound(self.track(channel).channel, args.path, "preset")

    def render(
        self,
        output: str | Path,
        *,
        bars: int | None = None,
        start_bar: int = 1,
        solo: Sequence[str | int] = (),
        mute: Sequence[str | int] = (),
        stems: Literal["channels", "mixer"] | Sequence[str | int] | None = None,
    ) -> Render | Stems:
        """Play the song and record the mix to ``output``: how to hear your edits.
        It plays in real time, so it takes about as long as what it records.

        ``bars``/``start_bar`` record only those bars (four beats a bar; the default is
        the whole song). ``solo`` channels are the only ones heard; ``mute`` channels
        are silenced; channels as for fl.track. FL's channel mutes are put back after.
        Returns a Render: the WAV's Path, so fl.read_wav(r) reads it, with r.span saying
        what played (start_bar, beats, tempo_bpm, seconds) and r.solo, r.mute.

        ``stems="channels"`` records each channel alone, ``stems="mixer"`` the channels
        on each mixer track together, one play each (so it takes that many times as
        long), into files named after ``output``; returns a list of Renders. Only
        channels with notes in those bars are recorded (and only the mixer tracks they
        feed); the list's ``.skipped`` names the rest by index. ``stems=["Bass", 3]``
        records exactly those channels, each alone, notes or not.
        """
        try:
            args = _RenderArgs(bars=bars, start_bar=start_bar, stems=stems)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.render", str(exc)) from None
        channels = {
            name: tuple(self.track(c).channel for c in ([value] if isinstance(value, str | int) else value))
            for name, value in (("solo", solo), ("mute", mute))
        }
        if args.stems is not None:
            if solo or mute:
                raise errors.invalid_argument("fl.render", "stems picks the channels itself; leave out solo and mute")
            if isinstance(args.stems, list):
                by, chosen = "channels", tuple(self.track(c).channel for c in args.stems)
            else:
                by, chosen = args.stems, ()
            receipt = self._session.render_stems(
                Path(output), by, start_bar=args.start_bar, bars=args.bars, channels=chosen
            )
            return Stems.of(receipt)
        return Render.of(
            self._session.render_wav(Path(output), start_bar=args.start_bar, bars=args.bars, **channels)
        )

    def export_offline(self, output: str | Path) -> Path:
        """Export the saved song with a second FL (about a minute). Licensed FL only; use render to listen."""
        return self._session.export_offline(Path(output))

    def save(self) -> None:
        """Save the song to disk. Never needed for edits to take effect or be heard:
        they are live in FL immediately. A trial FL Studio cannot save."""
        self._session.save_project()

    def read_wav(self, path: str | Path) -> tuple[np.ndarray, int]:
        """A WAV's samples, float (frames, channels), and its sample rate: a render, or one of
        FL's stock samples (fl.stock_folder / sound.path), which are Ogg Vorbis inside a WAV."""
        return read_wav(Path(path))

    def meters(self, bars: int = 2, start_bar: int = 1) -> CheckResult:
        """Play ``bars`` bars (1 to 8) from ``start_bar`` and report which channels
        made sound, from FL's own mixer meters: what a render of the mix cannot tell
        apart. Takes about that long; FL is stopped afterwards and nothing in the
        song changes.

        Returns each channel's name, ``sounded``, ``peak_dbfs`` and the ``reason``
        it was silent when FL shows one, plus ``summary``: one line to read.
        """
        return self._session.check(start_bar, bars)

    def undo(self) -> None:
        """Undo FL's last action. Notes this session wrote still read as written:
        write the track again to set its notes for certain."""
        self._session.undo()

    def file_edit(self) -> AbstractContextManager[None]:
        """Batch edits that rewrite the saved song (automation, effects) into one FL reopen.

        Outside a match only: during a match FL is never reopened, so this raises.
        Notes, tempo, parameters and levels apply live and need no batching::

            with fl.file_edit():
                fl.track("Bass").set_automation(points)
                fl.add_effect(0, "Fruity Parametric EQ 2")
        """
        # Each file-lane op rewrites the .flp and normally reloads FL - a full
        # restart; the batch reloads once on exit. Session mode refuses it.
        return self._session.file_edit()


class Track:
    """One channel's piano roll."""

    def __init__(self, fl: FL, channel: int, pattern: int = 1) -> None:
        try:
            args = _TargetArgs(channel=channel, pattern=pattern)
        except ValidationError as exc:
            raise errors.invalid_argument("Track", str(exc)) from None
        self.channel = args.channel
        self.pattern = args.pattern
        self._fl = fl

    def write(self, notes: Sequence[NoteInput], mode: NoteWriteMode = "merge") -> WriteResult:
        """Write notes to this track now; "merge" adds them, "replace" swaps the pattern's notes.

        Returns how many notes were written and the track's total afterwards.
        The notes are in FL when this returns: read them back with notes(),
        hear them with fl.render(). Raises if FL did not take the edit.
        """
        try:
            normalized_notes = _NOTES.validate_python(notes)
            normalized_mode = _WRITE_MODE.validate_python(mode)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.write", str(exc)) from None
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.write_notes(target, normalized_notes, normalized_mode)

    def clear(self) -> WriteResult:
        """Remove every note from this track's pattern."""
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.clear_notes(target)

    def notes(self) -> list[Note]:
        """The notes on this track's pattern as FL has them now, including your writes."""
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.read_notes(target)

    def set_automation(self, points: Sequence[AutomationPointInput]) -> AutomationResult:
        """Replace this channel's existing automation clip. Outside a match only."""
        try:
            normalized_points = _AUTOMATION_POINTS.validate_python(points)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.set_automation", str(exc)) from None
        return self._fl._session.set_automation(self.channel, normalized_points)

    def set_levels(
        self, *, volume: float | None = None, pan: float | None = None, pitch: int | None = None
    ) -> ChannelLevels:
        """Set this channel's volume (0..1), pan (-1..1) and/or pitch (semitones); returns FL's values."""
        try:
            args = _ChannelLevelsArgs(volume=volume, pan=pan, pitch=pitch)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.set_levels", str(exc)) from None
        if args.volume is None and args.pan is None and args.pitch is None:
            raise errors.invalid_argument(
                "Track.set_levels", "pass volume, pan, and/or pitch"
            )
        return self._fl._session.set_channel_levels(
            self.channel, volume=args.volume, pan=args.pan, pitch_semitones=args.pitch
        )

    def params(self, query: str | None = None, limit: int = 40) -> list[PluginParam]:
        """The instrument's parameters with their current values (normalized 0..1).
        ``query`` filters by name substring; ``limit`` caps how many are listed."""
        try:
            args = _ParamsArgs(query=query, limit=limit)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.params", str(exc)) from None
        return self._fl._session.plugin_params(self.channel, query=args.query, limit=args.limit)

    def set_param(self, param: int | str, value: float | str) -> PluginParam:
        """Set a parameter by index or name to a normalized value (0..1), or to a
        display/option string where the plugin's labels are known. Returns FL's
        value, which may snap to the control's own steps.
        """
        try:
            args = _ParamSetArgs(param=param, value=value)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.set_param", str(exc)) from None
        return self._fl._session.set_plugin_param(self.channel, args.param, args.value)
