"""Content-addressing for notes and note groups.

FL has no note identity, so we fingerprint by canonical *value*. This module is
the single source of truth for those hashes - the shadow score, the action log,
dedup, and diffing all key off exactly these functions, so a note has one id
everywhere. Hashes are salted with SCHEMA_VERSION: a schema bump changes every
id by construction, which is what invalidates stale caches safely.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .schemas import SCHEMA_VERSION, Note

_HASH_LEN = 16  # 64 bits of sha256; ample for per-project note sets


def note_hash(note: Note) -> str:
    """Stable id for one note's canonical value."""
    payload = {"v": SCHEMA_VERSION, **note.canonical()}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_HASH_LEN]


def noteset_hash(notes: Iterable[Note]) -> str:
    """Order-independent id for a group of notes.

    Built from the *sorted* member hashes, so re-writing the same notes in a
    different order yields the same id (identical rewrites dedupe), and two
    groups are equal iff their content is equal.
    """
    member_hashes = sorted(note_hash(n) for n in notes)
    blob = json.dumps({"v": SCHEMA_VERSION, "notes": member_hashes}, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_HASH_LEN]


def index_by_hash(notes: Iterable[Note]) -> dict[str, Note]:
    """Map each note to its hash. Last-writer-wins on exact-duplicate notes."""
    return {note_hash(n): n for n in notes}
