"""The shadow score — the server's own record of notes (docs/02 State model, docs/11).

FL Studio has no way to read notes back, so every NoteSet the server writes,
reads fresh, or the model hands it is stored here, content-addressed by a
hash of its canonical form (docs/11 "Naming things"): the same notes always
get the same id, so "the bassline from earlier" stays findable even after its
target has moved on to something else.

A second table maps each *target* (pattern, channel) to the id currently
believed to be there — that pointer is what drifts stale when the user edits
by hand, per the freshness policy in docs/11.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal

from . import hashing, paths, schemas
from .clock import now
from .spool import read_json, write_json_atomic

Provenance = Literal["shadow", "project", "model"]


def note_set_id(notes: list[schemas.Note]) -> str:
    """Content address for a note group.

    Delegates to the single source of truth in `hashing` (order-independent,
    SCHEMA_VERSION-salted) so the shadow score, action log, and diff layer all
    key off the exact same id. See docs/16.
    """
    return hashing.noteset_hash(notes)


def _target_key(target: schemas.Target) -> str:
    return f"{target.pattern}:{target.channel}"


class ShadowStore:
    """One project's shadow score: content-addressed NoteSets + target pointers.

    An MCP server dispatches tool calls concurrently (one worker thread per
    call), and one ShadowStore is shared across every call for a project, so
    every read-modify-write sequence (merge's read-then-append, then save())
    is guarded by a lock — otherwise two concurrent writes to the same target
    can race and silently drop one of them.
    """

    def __init__(self, project_key: str, root: Path | None = None) -> None:
        self.project_key = project_key
        self.root = root if root is not None else paths.project_dir(project_key)
        self.path = self.root / "shadow.json"
        self._lock = threading.Lock()
        self._by_id: dict[str, list[schemas.Note]] = {}
        self._targets: dict[str, schemas.NoteSetRef] = {}
        self.load()

    def write(
        self,
        target: schemas.Target,
        notes: list[schemas.Note],
        mode: Literal["merge", "replace"] = "merge",
        provenance: Provenance = "shadow",
    ) -> schemas.NoteSetRef:
        """Write-through update: call this on every successful add_notes."""
        with self._lock:
            if mode == "merge":
                base = self._notes_at_locked(target) or []
                notes = [*base, *notes]
            ref = self._store(notes, provenance)
            self._targets[_target_key(target)] = ref
            self._save_locked()
            return ref

    def clear(self, target: schemas.Target, provenance: Provenance = "shadow") -> schemas.NoteSetRef:
        with self._lock:
            ref = self._store([], provenance)
            self._targets[_target_key(target)] = ref
            self._save_locked()
            return ref

    def _store(self, notes: list[schemas.Note], provenance: Provenance) -> schemas.NoteSetRef:
        note_id = note_set_id(notes)
        self._by_id[note_id] = notes
        return schemas.NoteSetRef(id=note_id, provenance=provenance, as_of=now(), confidence=1.0, stale=False)

    def ref_at(self, target: schemas.Target) -> schemas.NoteSetRef | None:
        with self._lock:
            return self._targets.get(_target_key(target))

    def notes_at(self, target: schemas.Target) -> list[schemas.Note] | None:
        with self._lock:
            return self._notes_at_locked(target)

    def _notes_at_locked(self, target: schemas.Target) -> list[schemas.Note] | None:
        ref = self._targets.get(_target_key(target))
        return self._by_id.get(ref.id) if ref is not None else None

    def notes_by_id(self, note_id: str) -> list[schemas.Note] | None:
        """Look up a NoteSet by its fingerprint, independent of which target holds it now."""
        with self._lock:
            return self._by_id.get(note_id)

    def mark_stale(self, target: schemas.Target | None = None) -> None:
        """Layer-1 structure signal (docs/11): invalidate cached pointers.

        ``target=None`` marks the whole shadow score stale (e.g. OnProjectLoad).
        Note edits themselves don't fire this — only structural change signals do.
        """
        with self._lock:
            keys = [_target_key(target)] if target is not None else list(self._targets)
            for key in keys:
                ref = self._targets.get(key)
                if ref is not None:
                    self._targets[key] = ref.model_copy(update={"stale": True})
            self._save_locked()

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        raw = read_json(self.path)
        if raw is None:
            return
        self._by_id = {
            note_id: [schemas.Note.model_validate(n) for n in notes]
            for note_id, notes in raw.get("note_sets", {}).items()
        }
        self._targets = {
            key: schemas.NoteSetRef.model_validate(ref) for key, ref in raw.get("targets", {}).items()
        }

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        payload = {
            "schema_version": schemas.SCHEMA_VERSION,
            "note_sets": {
                note_id: [json.loads(n.model_dump_json()) for n in notes]
                for note_id, notes in self._by_id.items()
            },
            "targets": {key: json.loads(ref.model_dump_json()) for key, ref in self._targets.items()},
        }
        write_json_atomic(self.path, payload)
