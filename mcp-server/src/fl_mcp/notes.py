"""Note model and musical-unit conversion.

Per docs/06 the tool surface speaks *music* (beats, note names, 0..1 velocity)
and never exposes FL's internals. Ticks are deliberately absent here: the
executor inside FL knows the score's real PPQ at runtime (``flp.score.PPQ``),
so the spool carries beats and conversion happens there. This differs from
docs/03, which assumed the server would learn PPQ from a control-plane
handshake that v0 doesn't have yet — see docs/10.

Field-range validation (start/length/velocity/pan/color bounds) is owned by
``schemas.Note`` — the one seam every lane validates against; note-name -> MIDI
resolution is owned by ``pitch`` (the single parser). This module keeps only
what those can't: per-note error context (which index in the list failed).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from . import schemas
from .pitch import NoteError, key_name, parse_key

__all__ = ["NoteError", "describe", "key_name", "normalize_note", "normalize_notes", "parse_key"]


def _validation_message(exc: ValidationError) -> str:
    """First pydantic error, prefixed with its field name (e.g. 'velocity: ...')."""
    err = exc.errors()[0]
    field = ".".join(str(part) for part in err["loc"])
    return f"{field}: {err['msg']}" if field else err["msg"]


# schemas.Note.start has no upper bound (only ge=0) — it's meant to catch
# malformed/garbage input here, not to express a real musical limit, so this
# stays a local check rather than something to add to the shared seam.
_MAX_START_BEATS = 100_000.0


def normalize_note(raw: Any, index: int) -> dict[str, Any]:
    """Validate one note from the model and fill in defaults.

    Returns the wire form the executor consumes: beats for time, 0..1 for
    the continuous properties (which is exactly what flpianoroll's Note wants,
    so the executor does no rescaling).
    """
    if not isinstance(raw, dict):
        raise NoteError(f"note {index}: expected an object, got {raw!r}")

    missing = [f for f in ("key", "start", "length") if f not in raw]
    if missing:
        raise NoteError(f"note {index}: missing required field(s) {', '.join(missing)}")

    try:
        key = parse_key(raw["key"])
    except NoteError as exc:
        # Locate the offending note; "invalid note name 'nope'" is useless when
        # the model sent 64 of them.
        raise NoteError(f"note {index}: {exc}") from None

    try:
        note = schemas.Note(
            key=key,
            start=raw["start"],
            length=raw["length"],
            velocity=raw.get("velocity", 0.8),
            pan=raw.get("pan", 0.5),
            color=raw.get("color", 0),
        )
    except ValidationError as exc:
        raise NoteError(f"note {index}: {_validation_message(exc)}") from None

    if note.start > _MAX_START_BEATS:
        raise NoteError(f"note {index} start: must be at most {_MAX_START_BEATS:g}, got {note.start:g}")

    return note.canonical()


def normalize_notes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise NoteError(f"notes must be a list, got {type(raw).__name__}")
    if not raw:
        raise NoteError("notes list is empty — pass at least one note")
    if len(raw) > 5000:
        raise NoteError(f"too many notes ({len(raw)}); cap is 5000 per call")
    return [normalize_note(n, i) for i, n in enumerate(raw)]


def describe(notes: list[dict[str, Any]]) -> str:
    """Short human/model-readable summary, used in tool results."""
    if not notes:
        return "no notes"
    keys = sorted({n["key"] for n in notes})
    end = max(n["start"] + n["length"] for n in notes)
    span = ", ".join(key_name(k) for k in keys[:8]) + ("…" if len(keys) > 8 else "")
    return f"{len(notes)} notes over {end:g} beats ({span})"
