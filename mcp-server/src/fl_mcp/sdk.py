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

from pathlib import Path

from fl_mcp import errors
from fl_mcp.audio import AudioAnalysis, analyze_wav

from .schemas import (
    AutomationPoint,
    AutomationReceipt,
    Channel,
    Note,
    NoteWriteMode,
    PluginParam,
    SessionState,
    Target,
    WriteReceipt,
)
from .session import FLSession, create_session


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
        channels = self._session.channel_names()
        listing = ", ".join(f"{c.index}: {c.name}" for c in channels) or "(none)"
        if isinstance(name_or_index, int):
            if any(c.index == name_or_index for c in channels):
                return Track(self, name_or_index, pattern)
            raise ChannelNotFound(f"No channel at index {name_or_index}. Channels: {listing}")
        exact = [c for c in channels if c.name == name_or_index]
        if len(exact) == 1:
            return Track(self, exact[0].index, pattern)
        partial = [c for c in channels if name_or_index.lower() in c.name.lower()]
        if len(partial) == 1:
            return Track(self, partial[0].index, pattern)
        if len(exact) > 1 or len(partial) > 1:
            raise ChannelNotFound(f"{name_or_index!r} is ambiguous. Channels: {listing}")
        raise ChannelNotFound(f"No channel matching {name_or_index!r}. Channels: {listing}")

    def set_tempo(self, bpm: float) -> float:
        """Returns FL's own read-back, never the request echo."""
        return self._session.set_tempo(bpm)

    def render(self, output: str | Path) -> Path:
        """Export the current live project to ``output`` and return the confirmed WAV path."""
        return self._session.render_wav(Path(output))

    def analyze(self, wav: str | Path) -> AudioAnalysis:
        """Return deterministic audio measurements for a rendered WAV."""
        return analyze_wav(Path(wav))

    def play(self) -> None:
        self._session.play()

    def stop(self) -> None:
        self._session.stop()

    def undo(self) -> None:
        self._session.undo()

    def snapshot(self) -> SessionState:
        return self._session.snapshot()


class Track:
    """One channel's piano roll."""

    def __init__(self, fl: FL, channel: int, pattern: int = 1) -> None:
        self.channel = channel
        self.pattern = pattern
        self._fl = fl

    def write(self, notes: list[Note], mode: NoteWriteMode = "merge") -> WriteReceipt:
        """Applies NOW. Raises on failure; the receipt carries the confirmed
        note count (readback) and which backend applied."""
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.write_notes(target, notes, mode)

    def clear(self) -> WriteReceipt:
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.clear_notes(target)

    def notes(self) -> list[Note]:
        target = Target(pattern=self.pattern, channel=self.channel)
        return self._fl._session.read_notes(target)

    def set_automation(self, points: list[AutomationPoint]) -> AutomationReceipt:
        """Replace this channel's existing clip and return the saved-file readback."""
        return self._fl._session.set_automation(self.channel, points)

    def params(self, query: str | None = None, limit: int = 40) -> list[PluginParam]:
        """The channel generator's live parameters (E2: Sytrus exposes 597).
        ``query`` filters by substring; live-only - raises when FL is not
        connected, because plugin state has no honest file representation."""
        return self._fl._session.plugin_params(self.channel, query=query, limit=limit)

    def set_param(self, param: int | str, value: float) -> PluginParam:
        """Set one parameter (index, or unique name/substring), 0..1 normalized.
        Returns the READBACK - FL may quantize to the control's own grid."""
        return self._fl._session.set_plugin_param(self.channel, param, value)
