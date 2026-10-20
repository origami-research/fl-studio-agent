"""This session's note state and its durable action log. Pure data.

FL has no live note getter (PROBE_VERDICT Q1, re-checked 2026-10-18), so the
note state of a target this session wrote is the state FL confirmed for that
write; every other target is whatever the project held when FL loaded it,
and a later save of the project (FL saving its live state) supersedes the
records it postdates (docs/27 "Note state"). The state is in memory on purpose: it describes the
FL session this server drives, and a later server must not inherit claims
about an FL that has since been reset. The action log (before/after hashes
and the structured delta per write) is durable history. No FL IO here - the
session feeds it, tools read it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from fl_mcp.clock import now
from fl_mcp.fl import paths

from .schemas import (
    SCHEMA_VERSION,
    ActionEntry,
    Note,
    NoteDelta,
    NoteMove,
    NoteWriteMode,
    Target,
)

# A moved note must land within this window to pair as dragged rather than
# count as delete+insert. Beats for time, semitones for pitch.
MOVE_START_WINDOW_BEATS = 1.0
MOVE_PITCH_WINDOW_SEMITONES = 12

_HASH_LEN = 16  # 64 bits of sha256; ample for per-project note sets

# A pair sitting exactly on the window edge still IS a move; confidence floors
# here instead of hitting 0 (NoteMove requires confidence > 0).
_MIN_MOVE_CONFIDENCE = 0.001


class Store:
    """Per-session note state plus a durable action log. Thread-safe: MCP dispatches concurrently."""

    def __init__(self, project_key: str, root: Path | None = None) -> None:
        self.project_key = project_key
        self.root = root if root is not None else paths.project_dir(project_key)
        self.actions_path = self.root / "actions.jsonl"
        self._lock = threading.Lock()
        self._notes: dict[tuple[int, int], tuple[datetime, list[Note]]] = {}

    def record_write(
        self,
        target: Target,
        before: list[Note],
        after: list[Note],
        mode: NoteWriteMode,
        backend: str,
        command_id: int,
    ) -> NoteDelta:
        """Make ``after`` the target's state, then append the ActionEntry.

        The state is set first: the write already landed in FL, so a failing
        log append (raised to the caller) must not leave reads stale."""
        delta = diff_notes(before, after)
        with self._lock:
            self._notes[(target.pattern, target.channel)] = (now(), list(after))
            entry = ActionEntry(
                id=command_id,
                op="clear_notes" if not after and mode == "replace" else "add_notes",
                target=target,
                mode=mode,
                backend=backend,
                before_hash=noteset_hash(before),
                after_hash=noteset_hash(after),
                delta=delta,
                summary=(
                    f"+{len(delta.added)} -{len(delta.removed)} ~{len(delta.moved)}"
                    f" -> {len(after)} notes at {target.pattern}:{target.channel}"
                ),
            )
            self.root.mkdir(parents=True, exist_ok=True)
            with self.actions_path.open("a", encoding="utf-8") as f:
                f.write(entry.model_dump_json(by_alias=True))
                f.write("\n")
        return delta

    def notes_at(self, target: Target, saved_at: datetime | None = None) -> list[Note] | None:
        """The target's state if this session wrote it after ``saved_at`` (the
        project's last save, which holds everything FL had then), else None."""
        return self.written(saved_at).get((target.pattern, target.channel))

    def written(self, saved_at: datetime | None = None) -> dict[tuple[int, int], list[Note]]:
        """Every (pattern, channel) this session wrote after ``saved_at``, with its state."""
        with self._lock:
            return {
                key: list(notes)
                for key, (at, notes) in self._notes.items()
                if saved_at is None or at > saved_at
            }

    def history(self) -> Iterator[ActionEntry]:
        with self._lock:
            if not self.actions_path.is_file():
                return iter(())
            lines = self.actions_path.read_text(encoding="utf-8").splitlines()
        return (ActionEntry.model_validate_json(line) for line in lines if line.strip())

    def last_action(self) -> ActionEntry | None:
        entries = list(self.history())
        return entries[-1] if entries else None


# -- pure functions ---------------------------------------------------------------


def note_hash(note: Note) -> str:
    """Value hash of the canonical form - spelling-independent."""
    payload = {"v": SCHEMA_VERSION, **note.canonical()}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_HASH_LEN]


def noteset_hash(notes: list[Note]) -> str:
    """Order-independent hash of a note set."""
    member_hashes = sorted(note_hash(n) for n in notes)
    blob = json.dumps({"v": SCHEMA_VERSION, "notes": member_hashes}, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_HASH_LEN]


def diff_notes(old: list[Note], new: list[Note]) -> NoteDelta:
    """Structured delta: added/removed, with near-matches paired as moved."""
    old_by_hash = {note_hash(n): n for n in old}
    new_by_hash = {note_hash(n): n for n in new}
    removed = [n for h, n in old_by_hash.items() if h not in new_by_hash]
    added = [n for h, n in new_by_hash.items() if h not in old_by_hash]
    moved = _pair_moves(removed, added)
    paired_from = {note_hash(m.from_) for m in moved}
    paired_to = {note_hash(m.to) for m in moved}
    return NoteDelta(
        added=[n for n in added if note_hash(n) not in paired_to],
        removed=[n for n in removed if note_hash(n) not in paired_from],
        moved=moved,
    )


# -- the one helper ----------------------------------------------------------------


def _pair_moves(removed: list[Note], added: list[Note]) -> list[NoteMove]:
    """Greedy nearest-match within (MOVE_START_WINDOW_BEATS,
    MOVE_PITCH_WINDOW_SEMITONES) so a dragged note reads as moved."""
    candidates: list[tuple[float, Note, Note]] = []
    for old in removed:
        for new in added:
            start_delta = abs(old.start - new.start)
            pitch_delta = abs(old.key - new.key)
            if start_delta > MOVE_START_WINDOW_BEATS or pitch_delta > MOVE_PITCH_WINDOW_SEMITONES:
                continue
            # Normalized so 0 = identical position/pitch, 1 = at the window edge.
            distance = (
                start_delta / MOVE_START_WINDOW_BEATS + pitch_delta / MOVE_PITCH_WINDOW_SEMITONES
            ) / 2
            candidates.append((distance, old, new))
    candidates.sort(key=lambda c: c[0])  # closest first

    moves: list[NoteMove] = []
    paired_from: set[str] = set()
    paired_to: set[str] = set()
    for distance, old, new in candidates:
        old_hash, new_hash = note_hash(old), note_hash(new)
        if old_hash in paired_from or new_hash in paired_to:
            continue
        paired_from.add(old_hash)
        paired_to.add(new_hash)
        confidence = max(round(1.0 - distance, 3), _MIN_MOVE_CONFIDENCE)
        moves.append(NoteMove(from_=old, to=new, confidence=confidence))
    return moves
