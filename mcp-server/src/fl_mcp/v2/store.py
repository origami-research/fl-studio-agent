"""What we believe about the project: shadow score + action log. Pure data.

LANE K implements this module (docs/21), merging the essentials of v1's
records/actions/hashing/diff. Content-addressed note sets (hash of the
canonical form), a structured delta per write, provenance labels (shadow claim
vs project ground truth). No FL IO here - the session feeds it, tools read it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .schemas import ActionEntry, Note, NoteDelta, NoteMove, NoteSetRef, NoteWriteMode, Target

# A moved note must land within this window to pair as dragged rather than
# count as delete+insert. Beats for time, semitones for pitch.
MOVE_START_WINDOW_BEATS = 1.0
MOVE_PITCH_WINDOW_SEMITONES = 12


class Store:
    """Durable per-project memory. Thread-safe: MCP dispatches concurrently."""

    def __init__(self, project_key: str, root: Path | None = None) -> None:
        raise NotImplementedError("Lane K")

    def record_write(
        self,
        target: Target,
        notes: list[Note],
        mode: NoteWriteMode,
        backend: str,
        command_id: int,
    ) -> NoteDelta:
        """Apply the write to the shadow score, append the ActionEntry with
        before/after hashes, return the structured delta."""
        raise NotImplementedError("Lane K")

    def notes_at(self, target: Target) -> list[Note] | None:
        raise NotImplementedError("Lane K")

    def ref_at(self, target: Target) -> NoteSetRef | None:
        raise NotImplementedError("Lane K")

    def notes_by_id(self, ref_id: str) -> list[Note] | None:
        """Old note sets stay addressable by hash after the target moves on."""
        raise NotImplementedError("Lane K")

    def mark_ground_truth(self, target: Target, notes: list[Note]) -> NoteSetRef:
        """Record a project read-back: provenance 'project', not 'shadow'."""
        raise NotImplementedError("Lane K")

    def history(self) -> Iterator[ActionEntry]:
        raise NotImplementedError("Lane K")

    def last_action(self) -> ActionEntry | None:
        raise NotImplementedError("Lane K")


# -- pure functions ---------------------------------------------------------------


def note_hash(note: Note) -> str:
    """Value hash of the canonical form - spelling-independent."""
    raise NotImplementedError("Lane K")


def noteset_hash(notes: list[Note]) -> str:
    """Order-independent hash of a note set."""
    raise NotImplementedError("Lane K")


def diff_notes(old: list[Note], new: list[Note]) -> NoteDelta:
    """Structured delta: added/removed, with near-matches paired as moved."""
    raise NotImplementedError("Lane K")


def apply_delta(notes: list[Note], delta: NoteDelta) -> list[Note]:
    """A new list with the delta applied; the input is untouched."""
    raise NotImplementedError("Lane K")


# -- the one helper ----------------------------------------------------------------


def _pair_moves(removed: list[Note], added: list[Note]) -> list[NoteMove]:
    """Greedy nearest-match within (MOVE_START_WINDOW_BEATS,
    MOVE_PITCH_WINDOW_SEMITONES) so a dragged note reads as moved."""
    raise NotImplementedError("Lane K")
