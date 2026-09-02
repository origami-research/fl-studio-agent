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
    AutomationReceipt,
    Channel,
    ChannelLevels,
    Note,
    NoteWriteMode,
    PluginParam,
    SessionState,
    Target,
    WriteReceipt,
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
    """The project. Reads return confirmed state (store or live FL, labeled)."""

    def __init__(self, session: FLSession) -> None:
        self._session = session

    @property
    def channels(self) -> list[Channel]:
        return self._session.snapshot().channels

    @property
    def tempo(self) -> float:
        tempo = self._session.snapshot().tempo_bpm
        if tempo is None:
            raise errors.control_plane_unavailable("mixer.getCurrentTempo", 0.0)
        return tempo

    def track(self, name_or_index: str | int, pattern: int = 1) -> Track:
        """Resolve by index, exact name, then unambiguous substring; raises
        ChannelNotFound listing the real names otherwise. This is THE place
        channel names resolve - Target is ints-only past this boundary.
        ``pattern`` targets that pattern's roll (B3-3: PatNum is honored live).

        Resolution reads the PROJECT FILE, not the live snapshot: targets are
        file coordinates (that is what write_notes patches), and the file
        answers instantly while FL may be mid-boot right after a reload
        (dogfood A12: the live read raced every post-write reload)."""
        try:
            args = _TrackArgs(name_or_index=name_or_index, pattern=pattern)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.track", str(exc)) from exc
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
        """Returns FL's own read-back, never the request echo."""
        return self._session.set_tempo(bpm)

    def add_effect(self, insert_index: int, plugin_name: str) -> tuple[Effect, ...]:
        """Add a proven native effect to a mixer insert and return file readback.

        The current FL-authored evidence supports only Fruity Parametric EQ 2
        on the empty Master slot 0. Other placements or plugins refuse before
        synthesizing opaque plugin state.
        """
        try:
            args = _AddEffectArgs(insert_index=insert_index, plugin_name=plugin_name)
        except ValidationError as exc:
            raise errors.invalid_argument("fl.add_effect", str(exc)) from exc
        return self._session.add_effect(args.insert_index, args.plugin_name)

    def render(self, output: str | Path) -> Path:
        """Export the current live project to ``output`` and return the confirmed WAV path."""
        return self._session.render_wav(Path(output))

    def save(self) -> int | None:
        """Save the live project. [ASSUMED - live-verify]"""
        return self._session.save_project()

    def analyze(self, wav: str | Path) -> AudioAnalysis:
        """Return deterministic audio measurements for a rendered WAV."""
        return analyze_wav(Path(wav))

    def read_wav(self, path: str | Path) -> tuple[np.ndarray, int]:
        """Load PCM WAV samples and sample rate for NumPy processing."""
        return _read_wav(Path(path))

    def play(self) -> None:
        self._session.play()

    def stop(self) -> None:
        self._session.stop()

    def undo(self) -> None:
        self._session.undo()

    def snapshot(self) -> SessionState:
        return self._session.snapshot()

    def file_edit(self) -> AbstractContextManager[None]:
        """Batch file-lane edits (automation, channel levels, added plugins,
        and the file fallback for notes/tempo) into ONE FL reload.

        Each file-lane op rewrites the .flp and normally reloads FL - a full
        restart. Group them so the reload happens once::

            with fl.file_edit():
                fl.track("Bass").set_automation(points)
                fl.track("Kick").set_channel_levels(volume=0.9)

        Live ops (notes over the bridge, tempo/params over the wire) are
        unaffected - they never reload."""
        return self._session.file_edit()


class Track:
    """One channel's piano roll."""

    def __init__(self, fl: FL, channel: int, pattern: int = 1) -> None:
        try:
            args = _TargetArgs(channel=channel, pattern=pattern)
        except ValidationError as exc:
            raise errors.invalid_argument("Track", str(exc)) from exc
        self.channel = args.channel
        self.pattern = args.pattern
        self._fl = fl

    def write(self, notes: Sequence[NoteInput], mode: NoteWriteMode = "merge") -> WriteReceipt:
        """Applies NOW. Raises on failure; the receipt carries the confirmed
        note count (readback) and which backend applied."""
        try:
            normalized_notes = _NOTES.validate_python(notes)
            normalized_mode = _WRITE_MODE.validate_python(mode)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.write", str(exc)) from exc
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.write_notes(target, normalized_notes, normalized_mode)

    def clear(self) -> WriteReceipt:
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.clear_notes(target)

    def notes(self) -> list[Note]:
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.read_notes(target)

    def set_automation(self, points: Sequence[AutomationPointInput]) -> AutomationReceipt:
        """Replace this channel's existing clip and return the saved-file readback."""
        try:
            normalized_points = _AUTOMATION_POINTS.validate_python(points)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.set_automation", str(exc)) from exc
        return self._fl._session.set_automation(self.channel, normalized_points)

    def set_channel_levels(
        self, *, volume: float | None = None, pan: float | None = None, pitch: int | None = None
    ) -> ChannelLevels:
        """Set this channel's levels through the file lane and return its readback."""
        try:
            args = _ChannelLevelsArgs(volume=volume, pan=pan, pitch=pitch)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.set_channel_levels", str(exc)) from exc
        if args.volume is None and args.pan is None and args.pitch is None:
            raise errors.invalid_argument(
                "Track.set_channel_levels", "pass volume, pan, and/or pitch"
            )
        return self._fl._session.set_channel_levels(
            self.channel, volume=args.volume, pan=args.pan, pitch_semitones=args.pitch
        )

    def params(self, query: str | None = None, limit: int = 40) -> list[PluginParam]:
        """The channel generator's live parameters (E2: Sytrus exposes 597).
        ``query`` filters by substring; live-only - raises when FL is not
        connected, because plugin state has no honest file representation."""
        try:
            args = _ParamsArgs(query=query, limit=limit)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.params", str(exc)) from exc
        return self._fl._session.plugin_params(self.channel, query=args.query, limit=args.limit)

    def set_param(self, param: int | str, value: float | str) -> PluginParam:
        """Set by normalized value, or a model-backed display/option value.

        ``str`` values require captured metadata for this exact plugin. Returns
        FL's READBACK; FL may quantize to the control's own grid.
        """
        try:
            args = _ParamSetArgs(param=param, value=value)
        except ValidationError as exc:
            raise errors.invalid_argument("Track.set_param", str(exc)) from exc
        return self._fl._session.set_plugin_param(self.channel, args.param, args.value)
