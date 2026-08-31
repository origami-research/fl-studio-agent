"""What we believe about the project: shadow score + action log. Pure data.

LANE K implements this module (docs/21), merging the essentials of v1's
records/actions/hashing/diff. Content-addressed note sets (hash of the
canonical form), a structured delta per write, provenance labels (shadow claim
vs project ground truth). No FL IO here - the session feeds it, tools read it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path

from fl_mcp.clock import now
from fl_mcp.fl import paths

from .schemas import (
    SCHEMA_VERSION,
    ActionEntry,
    Note,
    NoteDelta,
    NoteMove,
    NoteSetRef,
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
    """Durable per-project memory. Thread-safe: MCP dispatches concurrently."""

    def __init__(self, project_key: str, root: Path | None = None) -> None:
        self.project_key = project_key
        self.root = root if root is not None else paths.project_dir(project_key)
        self.shadow_path = self.root / "shadow.json"
        self.actions_path = self.root / "actions.jsonl"
        self._lock = threading.Lock()
        self._by_id: dict[str, list[Note]] = {}
        self._targets: dict[str, NoteSetRef] = {}
        if self.shadow_path.is_file():
            # A corrupt shadow file raises here, loudly. Reading it as "empty"
            # would silently forget every claim this store ever recorded.
            raw = json.loads(self.shadow_path.read_text(encoding="utf-8"))
            self._by_id = {
                ref_id: [Note.model_validate(n) for n in notes]
                for ref_id, notes in raw["note_sets"].items()
            }
            self._targets = {
                key: NoteSetRef.model_validate(ref) for key, ref in raw["targets"].items()
            }

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
        with self._lock:
            before = self._notes_at_locked(target) or []
            after = [*before, *notes] if mode == "merge" else list(notes)
            delta = diff_notes(before, after)
            ref = self._store_locked(target, after, provenance="shadow")
            entry = ActionEntry(
                id=command_id,
                op="clear_notes" if not notes and mode == "replace" else "add_notes",
                target=target,
                mode=mode,
                backend=backend,
                before_hash=noteset_hash(before),
                after_hash=ref.id,
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
            self._save_locked()
            return delta

    def notes_at(self, target: Target) -> list[Note] | None:
        with self._lock:
            notes = self._notes_at_locked(target)
            return list(notes) if notes is not None else None

    def ref_at(self, target: Target) -> NoteSetRef | None:
        with self._lock:
            return self._targets.get(f"{target.pattern}:{target.channel}")

    def notes_by_id(self, ref_id: str) -> list[Note] | None:
        """Old note sets stay addressable by hash after the target moves on."""
        with self._lock:
            notes = self._by_id.get(ref_id)
            return list(notes) if notes is not None else None

    def mark_ground_truth(self, target: Target, notes: list[Note]) -> NoteSetRef:
        """Record a project read-back: provenance 'project', not 'shadow'."""
        with self._lock:
            ref = self._store_locked(target, list(notes), provenance="project")
            self._save_locked()
            return ref

    def history(self) -> Iterator[ActionEntry]:
        with self._lock:
            if not self.actions_path.is_file():
                return iter(())
            lines = self.actions_path.read_text(encoding="utf-8").splitlines()
        return (ActionEntry.model_validate_json(line) for line in lines if line.strip())

    def last_action(self) -> ActionEntry | None:
        entries = list(self.history())
        return entries[-1] if entries else None

    def _notes_at_locked(self, target: Target) -> list[Note] | None:
        ref = self._targets.get(f"{target.pattern}:{target.channel}")
        return self._by_id.get(ref.id) if ref is not None else None

    def _store_locked(self, target: Target, notes: list[Note], provenance: str) -> NoteSetRef:
        ref = NoteSetRef(id=noteset_hash(notes), target=target, provenance=provenance, as_of=now())
        self._by_id[ref.id] = notes
        self._targets[f"{target.pattern}:{target.channel}"] = ref
        return ref

    def _save_locked(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "note_sets": {
                ref_id: [n.model_dump(mode="json") for n in notes]
                for ref_id, notes in self._by_id.items()
            },
            "targets": {key: ref.model_dump(mode="json") for key, ref in self._targets.items()},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.shadow_path.with_name(self.shadow_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.shadow_path)


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


def apply_delta(notes: list[Note], delta: NoteDelta) -> list[Note]:
    """A new list with the delta applied; the input is untouched."""
    by_hash = {note_hash(n): n for n in notes}
    for note in delta.removed:
        by_hash.pop(note_hash(note), None)
    for move in delta.moved:
        by_hash.pop(note_hash(move.from_), None)
        by_hash[note_hash(move.to)] = move.to
    for note in delta.added:
        by_hash[note_hash(note)] = note
    return list(by_hash.values())


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
