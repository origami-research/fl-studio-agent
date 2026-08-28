"""Lane K test seam: a fake FLSession wired to fakes, per docs/21.

``sdk.connect(fake)`` is the deliberate injection point. FL IO is faked; the
Store is REAL (on tmp_path) so the write-through path - record_write, deltas,
the action log - is the shipped code, not a mock of it.
"""

from __future__ import annotations

import pytest

from fl_mcp.v2.schemas import (
    Channel,
    ControlResponse,
    Note,
    NoteWriteMode,
    SessionState,
    Target,
    WriteReceipt,
)
from fl_mcp.v2.session import FLSession
from fl_mcp.v2.store import Store


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
        self.control = control
        self.store = store
        self.project = None
        self.tempo = tempo
        self.fail_next_write: Exception | None = None
        self._channels = list(channels)
        self._next_id = 0

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        if self.fail_next_write is not None:
            exc, self.fail_next_write = self.fail_next_write, None
            raise exc
        self._next_id += 1
        delta = self.store.record_write(target, notes, mode, "project-write", self._next_id)
        confirmed = self.store.notes_at(target) or []
        return WriteReceipt(
            backend="project-write", target=target, confirmed_notes=len(confirmed), delta=delta
        )

    def clear_notes(self, target: Target) -> WriteReceipt:
        return self.write_notes(target, [], "replace")

    def read_notes(self, target: Target) -> list[Note]:
        return self.store.notes_at(target) or []

    def set_tempo(self, bpm: float) -> float:
        # FL stores tempo as u32 BPM x 1000; the readback is that quantized
        # value, not the caller's echo.
        self.tempo = round(bpm * 1000) / 1000
        return self.tempo

    def snapshot(self) -> SessionState:
        return SessionState(tempo_bpm=self.tempo, channels=list(self._channels))


DEFAULT_CHANNELS = (
    Channel(index=0, name="Kick"),
    Channel(index=1, name="Bass"),
    Channel(index=2, name="Lead"),
)


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def fake_session(tmp_path, control) -> FakeSession:
    return FakeSession(store=Store("test", root=tmp_path), channels=DEFAULT_CHANNELS, control=control)
