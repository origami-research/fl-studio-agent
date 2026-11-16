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

import itertools
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from fl_mcp import errors, guides
from fl_mcp.audio import read_wav
from fl_mcp.contract import SceneNotesResult, SceneSummary

from .kernel.wire import MAX_AUDIO_BYTES
from .schemas import (
    MIXER_HEADING,
    AutomationPoint,
    AutomationTake,
    Channel,
    ChannelLevels,
    ChannelPreset,
    ChannelRoute,
    ChannelSound,
    CheckResult,
    LoadKind,
    MixerEffect,
    MixerSend,
    MixerTrack,
    Note,
    NoteWriteMode,
    PatternInfo,
    PlacedClip,
    PlaylistClip,
    PluginParam,
    RenderReceipt,
    RenderSpan,
    Sound,
    StemsReceipt,
    Target,
    WriteResult,
)
from .session import FLSession, create_session
from .sounds import SEARCH_LIMIT_MAX, is_library_path

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


class _RouteArgs(BaseModel):
    mixer_track: SdkChannel


class _EffectArgs(BaseModel):
    mixer_track: SdkChannel
    slot: Annotated[int, Field(strict=True, ge=0, le=9)]


class _SendArgs(BaseModel):
    src_track: SdkChannel
    dst_track: SdkChannel
    level: NormalizedValue | None = None
    on: Annotated[bool, Field(strict=True)] = True


# A pattern by number or name; a PatternInfo from fl.new_pattern or fl.patterns stands for its number.
SdkPatternRef = Annotated[
    SdkPattern | Annotated[str, Field(strict=True, min_length=1)],
    BeforeValidator(lambda value: value.number if isinstance(value, PatternInfo) else value),
]
SdkBar = Annotated[int, Field(strict=True, ge=1)]


class _NewPatternArgs(BaseModel):
    name: Annotated[str, Field(strict=True, min_length=1)]
    copy_of: SdkPatternRef | None = None


class _RenamePatternArgs(BaseModel):
    pattern: SdkPatternRef
    name: Annotated[str, Field(strict=True, min_length=1)]


class _PlaceArgs(BaseModel):
    pattern: SdkPatternRef
    bars: SdkBar | Annotated[list[SdkBar], Field(min_length=1)]


class _AutomateArgs(BaseModel):
    points: Annotated[list[AutomationPoint], Field(min_length=2)]
    bar: SdkBar
    param: SdkChannel | Annotated[str, Field(strict=True, min_length=1)]
    mixer_track: SdkChannel | None = None
    slot: Annotated[int, Field(strict=True, ge=0, le=9)] | None = None
    pattern: SdkPatternRef | None = None

    @model_validator(mode="after")
    def _straight_lines(self) -> _AutomateArgs:
        positions = [p.position for p in self.points]
        if positions[0] != 0:
            raise ValueError("the first point is at beat 0, where the automation starts")
        if any(b <= a for a, b in itertools.pairwise(positions)):
            raise ValueError("point positions must increase")
        if any(p.tension for p in self.points):
            raise ValueError("points are joined by straight lines; leave out tension")
        if any(not 0 <= p.value <= 1 for p in self.points):
            raise ValueError("values are 0..1 across the control's range")
        return self


class _SetPresetArgs(BaseModel):
    preset: SdkChannel | Annotated[str, Field(strict=True, min_length=1)]


class _SoundsArgs(BaseModel):
    query: Annotated[str, Field(strict=True)] = ""
    kind: LoadKind | None = None
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

    kind: LoadKind
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
    """A rendered WAV, which is itself the WAV's Path (r.name, str(r); open it, read it with
    fl.read_wav, pass it to soundfile), and also says what played: ``span`` (start_bar,
    beats, tempo_bpm, seconds), the channels ``solo``-ed and ``mute``-d for it, and
    ``silent``: True when nothing in it reached -70 LUFS, as a note outside a sampled
    instrument's keys or a channel with nothing to play records."""

    span: RenderSpan
    solo: tuple[int, ...]
    mute: tuple[int, ...]
    silent: bool

    @classmethod
    def of(cls, receipt: RenderReceipt) -> Render:
        render = cls(receipt.path)
        render.span, render.solo, render.mute = receipt.span, tuple(receipt.solo), tuple(receipt.mute)
        render.silent = receipt.silent
        return render

    def __repr__(self) -> str:
        span = getattr(self, "span", None)
        if span is None:  # a path derived from a render (.parent, / "x") is a plain path
            return super().__repr__()
        heard = "".join(f", {k}={v!r}" for k, v in (("solo", self.solo), ("mute", self.mute)) if v)
        silent = ", silent=True" if self.silent else ""
        return f"Render({str(self)!r}, span={span!r}{heard}{silent})"


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


class Channels(list[Channel]):
    """fl.channels: a list of Channel, printed one per line."""

    def __repr__(self) -> str:
        lines = [f"  {c._text()}" for c in self]
        return "\n".join(["Channels (index 'name' -> mixer track, volume):", *lines])


class MixerTracks(list[MixerTrack]):
    """fl.mixer: the mixer tracks in use, printed one per line, and ``empty``: how many
    of FL's other mixer tracks are empty (no channel, effect, send or name of their own)."""

    empty: int = 0

    def __repr__(self) -> str:
        lines = [f"  {t._text()}" for t in self]
        return "\n".join([
            MIXER_HEADING,
            *lines,
            f"  (+ {self.empty} empty mixer tracks)",
        ])


class ChannelNotFound(ValueError):
    """Raised with the real channel names so the caller can pick one."""


def connect(session: FLSession | None = None) -> FL:
    """The entrypoint. Builds the default session (``create_session()``) when
    none is injected; tests inject one wired to fakes."""
    return FL(session if session is not None else create_session())


class FL:
    """The live FL Studio session: every call applies to FL at once and returns what FL
    now has, or raises with the reason. A channel is its index, exact name or a unique
    part of its name."""

    def __init__(self, session: FLSession) -> None:
        self._session = session

    @property
    def channels(self) -> Channels:
        """The channels in FL now, each with what the project's notes say it is for (``about``;
        print(fl.scene()) shows it, a channel's printout leaves it out)."""
        return Channels(self._session.live_channels())

    @property
    def mixer(self) -> MixerTracks:
        """FL's mixer tracks in use now (0 is the master): index, name, volume, pan, muted,
        solo, effects (each an Effect: slot, plugin, params() and set_param()) and sends
        (target and level). A track is in use when a channel plays into it, it holds an
        effect, it was renamed, or a track in use sends to it; ``empty`` counts the others."""
        tracks = MixerTracks(
            track.model_copy(update={"effects": [Effect._bound(self, e) for e in track.effects]})
            for track in self._session.mixer_tracks()
        )
        tracks.empty = self._session.mixer_track_count() - len(tracks)
        return tracks

    @property
    def playlist(self) -> list[PlaylistClip]:
        """The playlist's clips: those in the song FL opened, then those added this session
        with fl.place and fl.automate: track (None when FL did not report it), start and
        length in beats, and the pattern (or audio channel) each plays. FL cannot report
        its clips, so a clip changed in FL's own window is not seen."""
        return self._session.playlist()

    @property
    def patterns(self) -> list[PatternInfo]:
        """FL's patterns now: number, name and length in beats. In a new project,
        pattern 1 already starts the song."""
        return self._session.patterns()

    def scene(
        self, detail: Literal["summary", "notes"] = "summary", channel: str | int | None = None, pattern: int | None = None
    ) -> SceneSummary | SceneNotesResult:
        """The whole project in one digest: print(fl.scene()) shows the project's notes,
        tempo and key, the playlist, and a line per pattern, channel and mixer track in use
        (a SceneSummary, whose fields hold the same parts as data). detail="notes" is every
        note (by channel and pattern), optionally of one channel and one pattern. FL cannot
        report the notes, the playlist's clips, the automation clips or the project's notes,
        so those come from the song FL opened with this session's edits (as Track.notes and
        fl.playlist read them); the rest is FL's own answer, and when FL does not answer
        this raises."""
        if detail == "summary":
            return self._session.scene_summary()
        if detail != "notes":
            raise errors.invalid_argument("fl.scene", f'detail must be "summary" or "notes", got {detail!r}')
        return self._session.scene_notes(None if channel is None else self.track(channel).channel, pattern)

    @property
    def tempo(self) -> float:
        """FL's tempo now, in BPM."""
        return self._session.tempo()

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

    def new_pattern(self, name: str, copy_of: int | str | PatternInfo | None = None) -> PatternInfo:
        """Make a new pattern named ``name``: FL's next empty one, or a copy of the pattern
        ``copy_of`` (number, name or PatternInfo) with its notes. Write notes to it with
        fl.track(channel, pattern=number); put it in the song with fl.place."""
        try:
            args = _NewPatternArgs(name=name, copy_of=copy_of)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.new_pattern", str(exc)) from None
        return self._session.new_pattern(args.name, args.copy_of)

    def rename_pattern(self, pattern: int | str | PatternInfo, name: str) -> PatternInfo:
        """Rename a pattern (by number or name); returns the name FL now shows."""
        try:
            args = _RenamePatternArgs(pattern=pattern, name=name)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.rename_pattern", str(exc)) from None
        return self._session.rename_pattern(args.pattern, args.name)

    def place(self, pattern: int | str | PatternInfo, bars: int | Sequence[int]) -> list[PlacedClip]:
        """Put a pattern (number, name or PatternInfo) in the song at each of ``bars``: FL adds a clip
        of it there, on a playlist track it picks, and the song plays it from that bar.
        Give every bar a pattern plays at in one call: FL adds a pattern's clip only
        before its first clip, so a later bar for a pattern already in the song is
        refused (place a copy from fl.new_pattern(name, copy_of=...) there instead).
        FL plays a moment from each bar to add it, and leaves song mode on so a render
        plays the song. Clips cannot be moved or removed. Returns each clip with the track
        FL newly reports playing at that bar; track None means FL reported none, so that
        clip is not confirmed (FL reports only some clips of notes)."""
        try:
            args = _PlaceArgs(pattern=pattern, bars=bars if isinstance(bars, int) else list(bars))
        except ValidationError as exc:
            raise errors.invalid_argument("fl.place", str(exc)) from None
        bars_list = [args.bars] if isinstance(args.bars, int) else args.bars
        return self._session.place(args.pattern, bars_list)

    def automate(
        self,
        points: Sequence[AutomationPointInput],
        *,
        bar: int,
        param: int | str = "volume",
        channel: str | int | None = None,
        mixer_track: int | None = None,
        slot: int | None = None,
        pattern: int | str | PatternInfo | None = None,
    ) -> AutomationTake:
        """Move a control over time from ``bar``: a filter sweep, a volume ride, a riser.
        ``points`` are {"position": beats from bar, "value": 0..1} joined by straight
        lines, the first at beat 0; values span the control's range (pan: 0.5 centre).

        The control: ``channel`` (as for fl.track) with param "volume", "pan" or one of
        its instrument's parameters (index or name, see Track.params); or ``mixer_track``
        (0 is the master) with "volume" or "pan", or with ``slot`` for a parameter of the
        effect in that slot (see fl.mixer).

        FL records the moves into a new pattern named after the control and puts it in
        the song at ``bar``; to change them, automate again with ``pattern`` set to that
        pattern at the same bar, which replaces the moves over the beats you give. It
        plays in real time twice (recording, then playing back to check), so it takes
        about twice as long as the automation. Returns FL's playback: ``points`` and
        ``max_error``, and the ``pattern``."""
        try:
            args = _AutomateArgs(
                points=_AUTOMATION_POINTS.validate_python(points),
                bar=bar,
                param=param,
                mixer_track=mixer_track,
                slot=slot,
                pattern=pattern,
            )
        except ValidationError as exc:
            raise errors.invalid_argument("fl.automate", str(exc)) from None
        index = None if channel is None else self.track(channel).channel
        return self._session.automate(
            args.points, args.bar, args.param, index, args.mixer_track, args.slot, args.pattern
        )

    def effect(self, mixer_track: int, slot: int) -> Effect:
        """The effect in ``slot`` (0..9) of mixer track ``mixer_track`` (0 is the master),
        the same Effect fl.mixer and fl.load_preset(...).effect give: ``params()`` and
        ``set_param()`` as for a track's instrument, with FL's own text for each value
        ("-35.4dB", "20.0:1") in ``display``. Raises when the slot holds no effect::

            fx = fl.load_preset("Pad", "Plugin presets/Effects/Fruity Reeverb 2/Default.fst").effect
            fx.set_param("Decay time", 0.3)
            fl.effect(fx.mixer_track, fx.slot).params("decay")

        A compressor's sidechain input is not among its parameters: FL sets it only in the
        plugin's window, so code cannot key one effect from another track; draw pumping as a
        dip on each kick with fl.automate on the part's mixer track volume.
        """
        try:
            args = _EffectArgs(mixer_track=mixer_track, slot=slot)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.effect", str(exc)) from None
        return Effect._bound(self, self._session.effect(args.mixer_track, args.slot))

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
        self,
        query: str = "",
        like: Sound | str | Path | None = None,
        kind: LoadKind | None = None,
        plugin: str | None = None,
        limit: int = 20,
    ) -> list[Sound]:
        """The library's sounds ranked by how they sound: FL's stock samples and instrument
        presets, and the CC0 packs' samples ("CC0/..."), best match first.

        ``query`` describes the sound ("punchy dry house kick", "warm analog pad slow
        attack"); ``like`` is a reference to match instead or as well: a Sound, a library
        path, or an audio file you wrote (an fl.audition render, a chop). Each Sound has
        ``kind`` ("preset" or "sample"), ``name``, ``plugin``, ``path``, ``score`` (by
        sound, higher is closer; None when it matched by name only: an effect preset,
        which makes no sound alone, or a sound the index lacks; those come first, so
        check for None before rounding or sorting by score) and measurements:
        ``seconds``, ``pitch`` (a sample's MIDI note, None when unpitched), ``decay``
        (seconds to 30 dB down), ``brightness`` (Hz), ``low`` and ``high`` (energy share
        below 150 Hz, above 5 kHz). A preset is measured by its preview, the default
        fl.audition phrase. ``kind`` keeps one kind; ``plugin`` that plugin's presets;
        ``limit`` caps the list (at most 200). No query and no ``like``: the library in
        folder order.

        Listen before choosing: fl.read_wav(sound) for a sample, fl.audition(sound) for
        a preset.
        """
        try:
            args = _SoundsArgs(query=query, kind=kind, plugin=plugin, limit=limit)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.sounds", str(exc)) from None
        like_path, like_audio = self._reference(like)
        return self._session.sounds(
            args.query, like_path=like_path, like_audio=like_audio, kind=args.kind, plugin=args.plugin, limit=args.limit
        )

    @staticmethod
    def _reference(like: Sound | str | Path | None) -> tuple[str | None, bytes | None]:
        """fl.sounds' ``like``: a library sound's path, or the bytes of a file you wrote."""
        if like is None:
            return None, None
        if isinstance(like, Sound):
            return like.path, None
        if not isinstance(like, str | Path) or not str(like):
            raise errors.invalid_argument("fl.sounds", f"like= takes a Sound, a library path or an audio file, not {like!r}")
        path = Path(like)
        if not path.is_file():
            return str(like), None
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise errors.invalid_argument("fl.sounds", f"{path.name} is over {MAX_AUDIO_BYTES >> 20} MB; pass a shorter file")
        return None, path.read_bytes()

    def load_sample(self, channel: str | int, sample: Sound | str | Path) -> ChannelSound:
        """Load a sample into a sampler channel: a Sound from ``fl.sounds(kind="sample")``
        or its path, or an audio file you wrote (WAV, FLAC, Ogg, AIFF), such as a chop
        or a stretched loop made with numpy or librosa. FL renames the channel after the
        file; returns the channel's name as FL now reports it. A plugin channel is
        refused (give it an instrument preset instead).

        A note plays the sample from its start, at its own pitch on C4 (MIDI 60) and
        resampled on other keys; some channels stop it when the note ends, so write the
        note as long as the sample. fl.guide("chops") makes one in Python.
        """
        if not isinstance(sample, Sound | str | Path):
            raise errors.invalid_argument(
                "fl.load_sample", f"pass a Sound from fl.sounds(), its path, or an audio file's path, not {sample!r}"
            )
        # Anything that is not a file you wrote is a library sound: a guessed name ("909 Kick") is
        # refused by the engine naming the closest real ones.
        if isinstance(sample, Sound) or not Path(sample).is_file():
            try:
                args = _LoadArgs(kind="sample", sound=sample if isinstance(sample, Sound) else str(sample))
            except ValidationError as exc:
                raise errors.invalid_argument("fl.load_sample", str(exc)) from None
            return self._session.load_sound(self.track(channel).channel, args.path, "sample")
        path = Path(sample)
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise errors.invalid_argument(
                "fl.load_sample", f"{path.name} is over {MAX_AUDIO_BYTES >> 20} MB; load a shorter or smaller file"
            )
        return self._session.load_audio(self.track(channel).channel, path.name, path.read_bytes())

    def load_preset(self, channel: str | int, preset: Sound | str) -> ChannelSound:
        """Load a stock preset into a channel: a Sound from ``fl.sounds(kind="preset")``,
        or its path. An instrument preset puts its plugin on the channel, replacing
        the instrument (a sampler too), and FL names the channel after the preset:
        the way to change a channel's instrument. An effect preset adds its effect to
        the next free slot of the mixer track the channel plays through: ``.effect``, an
        Effect whose params() and set_param() shape it.
        Returns the channel's name and instrument as FL now reports them.
        """
        try:
            args = _LoadArgs(kind="preset", sound=preset)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.load_preset", str(exc)) from None
        loaded = self._session.load_sound(self.track(channel).channel, args.path, "preset")
        if loaded.effect is None:
            return loaded
        return loaded.model_copy(update={"effect": Effect._bound(self, loaded.effect)})

    def audition(
        self, preset: Sound | str, notes: Sequence[NoteInput] | None = None, output: str | Path = "audition.wav"
    ) -> Render:
        """Hear an instrument preset before using it: load it on the Audition channel, play
        ``notes`` (default: a two-bar phrase from C3 to a C major chord) on that channel
        alone, and record it to ``output``; the Audition channel's notes are put back.
        Returns the Render (read it with fl.read_wav). Takes about the phrase's length in
        real time, plus loading the plugin. A sample needs no audition: fl.read_wav(sound).
        Do not fl.undo() after it: FL's undo would take back writing the notes back.
        """
        try:
            args = _LoadArgs(kind="preset", sound=preset)
            phrase = None if notes is None else _NOTES.validate_python(list(notes))
        except ValidationError as exc:
            raise errors.invalid_argument("fl.audition", str(exc)) from None
        audition = self._session.audition(args.path, Path(output), notes=phrase)
        return Render.of(audition.render)

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
        what played (start_bar, beats, tempo_bpm, seconds), r.solo, r.mute, and r.silent
        when it recorded silence (a solo or a range can: check before dividing by its
        level). A sampled instrument is silent on keys outside its samples. The WAV is
        written to the run's renders folder, not the working folder, so keep the Render:
        fl.read_wav("mix.wav") does not find it.

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

    def save(self) -> None:
        """Save the song to disk. Never needed for edits to take effect or be heard:
        they are live in FL immediately. A trial FL Studio cannot save."""
        self._session.save_project()

    def read_wav(self, path: str | Path | Sound) -> tuple[np.ndarray, int]:
        """Audio's samples, float (frames, channels), and its sample rate: a render, a file
        you wrote, or a library sample (a Sound from fl.sounds, or its path), so a sample
        can be heard before it is loaded. Reads FL's Ogg-in-WAV and WavPack stock samples."""
        if isinstance(path, Sound):
            return read_wav(self._session.sound_file(path.path))
        if Path(path).is_file():
            return read_wav(Path(path))
        if is_library_path(str(path)):
            return read_wav(self._session.sound_file(str(path)))
        # Neither a file here nor a library path: answered at once, without searching the
        # library for names like it (that search took 15 s on a fresh machine).
        raise errors.invalid_argument(
            "fl.read_wav",
            f"there is no file {str(path)!r}; a render is read through the Render fl.render returned, "
            "and a library sample is a Sound from fl.sounds() or its path",
        )

    def meters(self, bars: int = 2, start_bar: int = 1) -> CheckResult:
        """Play ``bars`` bars (1 to 8) from ``start_bar`` and report which channels
        made sound, from FL's own mixer meters: what a render of the mix cannot tell
        apart. Takes about that long; FL is stopped afterwards and nothing in the
        song changes.

        Returns each channel's name, ``sounded``, ``peak_dbfs`` and the ``reason``
        it was silent when FL shows one, plus ``summary``: one line to read.
        ``peak_dbfs`` (and the master's) is None when nothing was heard there, and
        ``sounded`` None for a channel playing straight into the master; check for None
        before doing arithmetic on a reading.
        """
        return self._session.check(start_bar, bars)

    def undo(self) -> None:
        """Undo FL's last action. Notes this session wrote still read as written:
        write the track again to set its notes for certain. After a render with solo or
        mute, write the track back instead: FL's undo holds the mute changes."""
        self._session.undo()

    def guide(self, name: str | None = None) -> None:
        """Optional reference notes on techniques and this environment: with no name, print
        the guides and what each is for; with a name, print that guide. Each ends with a
        worked example."""
        found = guides.load()
        if name is None:
            print(guides.index(found))
            return
        chosen = next((g for g in found if g.name == name), None)
        if chosen is None:
            names = [g.name for g in found]
            raise errors.ToolError(
                "unknown_target", f"No guide {name!r}.", f"Guides: {', '.join(names)}.", candidates=names
            )
        print(chosen.body)


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
        """The notes on this track's pattern: those in the song FL opened, with this
        session's writes applied. FL cannot report notes, so a note changed in FL's own
        windows is not seen."""
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.read_notes(target)

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
        ``query`` filters by name substring; ``limit`` caps how many are listed. A sampler
        channel has no instrument plugin, so no parameters, and raises: shape a sample with
        Track.set_levels or an effect on its mixer track (fl.load_preset, then .effect)."""
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


class Effect(MixerEffect):
    """One mixer effect, live: where it is (``mixer_track``, ``slot``), its ``plugin``, and its
    parameters through ``params()`` and ``set_param()``. fl.effect, fl.mixer's ``effects`` and
    fl.load_preset(...).effect all give one."""

    _fl: FL = PrivateAttr()

    @classmethod
    def _bound(cls, fl: FL, effect: MixerEffect) -> Effect:
        bound = cls(mixer_track=effect.mixer_track, slot=effect.slot, plugin=effect.plugin)
        bound._fl = fl
        return bound

    def params(self, query: str | None = None, limit: int = 40) -> list[PluginParam]:
        """The effect's parameters with their current values (normalized 0..1).
        ``query`` filters by name substring; ``limit`` caps how many are listed."""
        try:
            args = _ParamsArgs(query=query, limit=limit)
        except ValidationError as exc:
            raise errors.invalid_argument("Effect.params", str(exc)) from None
        return self._fl._session.effect_params(self.mixer_track, self.slot, query=args.query, limit=args.limit)

    def set_param(self, param: int | str, value: float | str) -> PluginParam:
        """Set a parameter by index or name to a normalized value (0..1). Returns FL's
        value, which may snap to the control's own steps."""
        try:
            args = _ParamSetArgs(param=param, value=value)
        except ValidationError as exc:
            raise errors.invalid_argument("Effect.set_param", str(exc)) from None
        return self._fl._session.set_effect_param(self.mixer_track, self.slot, args.param, args.value)
