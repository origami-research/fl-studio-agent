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
from typing import Annotated

import numpy as np
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from fl_mcp import errors
from fl_mcp.audio import AudioAnalysis, _read_wav, analyze_wav

from .flp import Effect
from .schemas import (
    AutomationPoint,
    AutomationResult,
    Channel,
    ChannelLevels,
    CheckResult,
    Note,
    NoteWriteMode,
    PluginParam,
    Target,
    WriteResult,
)
from .session import FLSession, create_session

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


_NOTES = TypeAdapter(list[Note])
_AUTOMATION_POINTS = TypeAdapter(list[AutomationPoint])
_WRITE_MODE = TypeAdapter(NoteWriteMode)


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
        """The channels in FL now."""
        return self._session.snapshot().channels

    @property
    def tempo(self) -> float:
        """FL's tempo now, in BPM."""
        tempo = self._session.tempo()
        if tempo is None:
            raise errors.control_plane_unavailable("mixer.getCurrentTempo", 0.0)
        return tempo

    def track(self, name_or_index: str | int, pattern: int = 1) -> Track:
        """A channel's piano roll in ``pattern``: by index, exact name, or a unique
        part of the name. Raises ChannelNotFound listing the channels otherwise.
        """
        # THE place channel names resolve - Target is ints-only past this
        # boundary. Resolution reads the PROJECT FILE, not the live snapshot: targets are
        # file coordinates (that is what write_notes patches), and the file
        # answers instantly while FL may be mid-boot right after a reload
        # (dogfood A12: the live read raced every post-write reload).
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
        """Add Fruity Parametric EQ 2 to the empty Master slot 0 (the one supported
        placement); returns the effects now on that insert. Outside a match only.
        """
        try:
            args = _AddEffectArgs(insert_index=insert_index, plugin_name=plugin_name)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.add_effect", str(exc)) from None
        return self._session.add_effect(args.insert_index, args.plugin_name)

    def render(self, output: str | Path) -> Path:
        """Play the song once and record the mix to ``output``: how to hear your edits.

        It takes about the song's length in real time.
        """
        return self._session.render_wav(Path(output)).path

    def export_offline(self, output: str | Path) -> Path:
        """Export the saved song with a second FL (about a minute). Licensed FL only; use render to listen."""
        return self._session.export_offline(Path(output))

    def save(self) -> None:
        """Save the song to disk. Never needed for edits to take effect or be heard:
        they are live in FL immediately. A trial FL Studio cannot save."""
        self._session.save_project()

    def analyze(self, wav: str | Path) -> AudioAnalysis:
        """Return deterministic audio measurements for a rendered WAV."""
        return analyze_wav(Path(wav))

    def read_wav(self, path: str | Path) -> tuple[np.ndarray, int]:
        """Load PCM WAV samples and sample rate for NumPy processing."""
        return _read_wav(Path(path))

    def check(self, bars: int = 2, start_bar: int = 1) -> CheckResult:
        """Play ``bars`` bars (1 to 8) from ``start_bar`` and report which channels
        made sound, from FL's own mixer meters. Takes about that long; FL is
        stopped afterwards and nothing in the song changes.

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

    def set_channel_levels(
        self, *, volume: float | None = None, pan: float | None = None, pitch: int | None = None
    ) -> ChannelLevels:
        """Set this channel's volume (0..1), pan (-1..1) and/or pitch (semitones); returns FL's values."""
        try:
            args = _ChannelLevelsArgs(volume=volume, pan=pan, pitch=pitch)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.set_channel_levels", str(exc)) from None
        if args.volume is None and args.pan is None and args.pitch is None:
            raise errors.invalid_argument(
                "Track.set_channel_levels", "pass volume, pan, and/or pitch"
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
