"""One object owning the connection to FL: control wire + note spool + project.

LANE S implements this module (docs/21). Explicit backend choice replaces
v1's router cascade: a note write goes to the live plane when connected, else
the project file (with FL reload), else raises NotePlaneUnavailable saying
exactly what to set up. Whichever backend applies, the write-through records
the same structured delta in the store, and every result is a READBACK.
"""

from __future__ import annotations

from pathlib import Path

from .schemas import (
    ChannelLevels,
    DoctorReport,
    Note,
    NoteWriteMode,
    SessionState,
    Target,
    WriteReceipt,
)
from .spool import Spool
from .store import Store
from .wire import MidiControl

# Right after a SIGKILL, LaunchServices still has the dead process registered;
# it clears in a second or two, so the relaunch waits before retrying.
RELAUNCH_SETTLE_SECONDS = 2.0


def create_session() -> FLSession:
    """THE factory, called once at the entry point (server lifespan or
    ``sdk.connect()``). Builds the wire (or None when python-rtmidi is missing
    or ports can't open - tools then degrade honestly), the spool, the store,
    and resolves the project file. No lazy construction hidden in getters."""
    raise NotImplementedError("Lane S")


class FLSession:
    """Everything below the SDK talks to FL through this one object."""

    control: MidiControl | None
    spool: Spool
    store: Store
    project: Path | None

    def __init__(
        self,
        *,
        control: MidiControl | None,
        spool: Spool,
        store: Store,
        project: Path | None,
    ) -> None:
        raise NotImplementedError("Lane S")

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        """Apply via the chosen backend, VERIFY BY READBACK, record the delta.
        Raises (NotePlaneUnavailable / WriteRejected) on failure - never
        returns a maybe, never falls through after a partial application."""
        raise NotImplementedError("Lane S")

    def clear_notes(self, target: Target) -> WriteReceipt:
        raise NotImplementedError("Lane S")

    def read_notes(self, target: Target) -> list[Note]:
        """Store first (with provenance); project-file readback on demand."""
        raise NotImplementedError("Lane S")

    def set_tempo(self, bpm: float) -> float:
        """Live-first (confirmed via getCurrentTempo readback, BPM x 1000);
        else the project file (flp.set_tempo) + reload. Returns the readback."""
        raise NotImplementedError("Lane S")

    def set_channel_levels(
        self,
        channel: int,
        *,
        volume: float | None = None,
        pan: float | None = None,
        pitch_semitones: int | None = None,
    ) -> ChannelLevels:
        """All-or-nothing per call: a field that landed is never silently
        discarded by a fallback (the v1 partial-then-reload bug)."""
        raise NotImplementedError("Lane S")

    def snapshot(self) -> SessionState:
        raise NotImplementedError("Lane S")

    def doctor(self) -> DoctorReport:
        """Typed report - control plane, note plane, project file - with a
        concrete ``fix`` string per degraded plane."""
        raise NotImplementedError("Lane S")

    # -- the one helper ---------------------------------------------------------

    def _reload_fl(self) -> None:
        """UGLY(U5), quarantined: FL has no 'reopen this file' API and a polite
        quit blocks on save dialogs. SIGKILL, wait RELAUNCH_SETTLE_SECONDS,
        then ``open`` with bounded retries."""
        raise NotImplementedError("Lane S")
