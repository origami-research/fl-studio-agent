"""Note model and musical-unit conversion.

Per docs/06 the tool surface speaks *music* (beats, note names, 0..1 velocity)
and never exposes FL's internals. Ticks are deliberately absent here: the
executor inside FL knows the score's real PPQ at runtime (``flp.score.PPQ``),
so the spool carries beats and conversion happens there. This differs from
docs/03, which assumed the server would learn PPQ from a control-plane
handshake that v0 doesn't have yet — see docs/10.

Field-range validation (start/length/velocity/pan/color bounds) is owned by
``schemas.Note`` — the one seam every lane validates against. This module
keeps only what pydantic can't do for us: note-name -> MIDI resolution
(``parse_key``), and per-note error context (which index in the list failed).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from . import schemas

# Scientific pitch notation: C4 == MIDI 60 == middle C.
#
# Heads up: FL Studio's piano roll *labels* MIDI 60 as "C5" by default, because
# its display uses a +1 octave offset. We follow the MIDI standard here and call
# it C4. If an octave lands wrong, pass a raw MIDI integer instead of a name.
_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_MIDDLE_C_OCTAVE = 4


class NoteError(ValueError):
    """Raised for malformed note input, with a message meant for the model."""


def parse_key(key: Any) -> int:
    """Accept a MIDI number (0-127) or a note name like 'C4', 'F#3', 'Bb5'."""
    if isinstance(key, bool):
        raise NoteError(f"invalid key {key!r}: expected a note name or MIDI number")
    if isinstance(key, int):
        if not 0 <= key <= 127:
            raise NoteError(f"MIDI note {key} out of range (0-127)")
        return key
    if isinstance(key, float):
        if key != int(key):
            raise NoteError(f"MIDI note {key} must be a whole number")
        return parse_key(int(key))
    if not isinstance(key, str) or not key.strip():
        raise NoteError(f"invalid key {key!r}: expected a note name or MIDI number")

    text = key.strip()
    letter = text[0].upper()
    if letter not in _SEMITONES:
        raise NoteError(f"invalid note name {key!r}: must start with A-G")

    semitone = _SEMITONES[letter]
    idx = 1
    while idx < len(text) and text[idx] in "#b♯♭":
        semitone += 1 if text[idx] in "#♯" else -1
        idx += 1

    octave_text = text[idx:]
    if not octave_text:
        raise NoteError(f"invalid note name {key!r}: missing octave, e.g. 'C4'")
    try:
        octave = int(octave_text)
    except ValueError:
        raise NoteError(f"invalid note name {key!r}: '{octave_text}' is not an octave") from None

    midi = semitone + (octave - _MIDDLE_C_OCTAVE) * 12 + 60
    if not 0 <= midi <= 127:
        raise NoteError(f"note {key!r} resolves to MIDI {midi}, outside 0-127")
    return midi


def key_name(midi: int) -> str:
    """Inverse of parse_key, for echoing back what we understood."""
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[midi % 12]}{midi // 12 - 5 + _MIDDLE_C_OCTAVE}"


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
