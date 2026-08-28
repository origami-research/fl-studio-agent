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

from .schemas import Channel, Note, NoteWriteMode, SessionState, WriteReceipt
from .session import FLSession


class ChannelNotFound(ValueError):
    """Raised with the real channel names so the caller can pick one."""


def connect(session: FLSession | None = None) -> FL:
    """The entrypoint. Builds the default session (``create_session()``) when
    none is injected; tests inject one wired to fakes."""
    raise NotImplementedError("Lane K")


class FL:
    """The project. Reads return confirmed state (store or live FL, labeled)."""

    def __init__(self, session: FLSession) -> None:
        raise NotImplementedError("Lane K")

    @property
    def channels(self) -> list[Channel]:
        raise NotImplementedError("Lane K")

    @property
    def tempo(self) -> float:
        raise NotImplementedError("Lane K")

    def track(self, name_or_index: str | int) -> Track:
        """Resolve by index, exact name, then unambiguous substring; raises
        ChannelNotFound listing the real names otherwise. This is THE place
        channel names resolve - Target is ints-only past this boundary."""
        raise NotImplementedError("Lane K")

    def set_tempo(self, bpm: float) -> float:
        """Returns FL's own read-back, never the request echo."""
        raise NotImplementedError("Lane K")

    def play(self) -> None:
        raise NotImplementedError("Lane K")

    def stop(self) -> None:
        raise NotImplementedError("Lane K")

    def undo(self) -> None:
        raise NotImplementedError("Lane K")

    def snapshot(self) -> SessionState:
        raise NotImplementedError("Lane K")


class Track:
    """One channel's piano roll."""

    def __init__(self, fl: FL, channel: int, pattern: int = 1) -> None:
        raise NotImplementedError("Lane K")

    def write(self, notes: list[Note], mode: NoteWriteMode = "merge") -> WriteReceipt:
        """Applies NOW. Raises on failure; the receipt carries the confirmed
        note count (readback) and which backend applied."""
        raise NotImplementedError("Lane K")

    def clear(self) -> WriteReceipt:
        raise NotImplementedError("Lane K")

    def notes(self) -> list[Note]:
        raise NotImplementedError("Lane K")
