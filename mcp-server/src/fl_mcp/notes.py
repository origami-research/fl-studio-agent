"""Note model and musical-unit conversion.

Per docs/06 the tool surface speaks *music* (beats, note names, 0..1 velocity)
and never exposes FL's internals. Ticks are deliberately absent here: the
executor inside FL knows the score's real PPQ at runtime (``flp.score.PPQ``),
so the spool carries beats and conversion happens there. This differs from
docs/03, which assumed the server would learn PPQ from a control-plane
handshake that v0 doesn't have yet — see docs/10.
"""

from __future__ import annotations

from typing import Any

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


def _num(value: Any, field: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NoteError(f"{field} must be a number, got {value!r}")
    if not lo <= value <= hi:
        raise NoteError(f"{field} must be between {lo} and {hi}, got {value}")
    return float(value)


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

    length = _num(raw["length"], f"note {index} length", 0.0, 1024.0)
    if length <= 0:
        raise NoteError(f"note {index}: length must be greater than 0 beats")

    try:
        key = parse_key(raw["key"])
    except NoteError as exc:
        # Locate the offending note; "invalid note name 'nope'" is useless when
        # the model sent 64 of them.
        raise NoteError(f"note {index}: {exc}") from None

    return {
        "key": key,
        "start": _num(raw["start"], f"note {index} start", 0.0, 100_000.0),
        "length": length,
        "velocity": _num(raw.get("velocity", 0.8), f"note {index} velocity", 0.0, 1.0),
        "pan": _num(raw.get("pan", 0.5), f"note {index} pan", 0.0, 1.0),
        "color": int(_num(raw.get("color", 0), f"note {index} color", 0, 15)),
    }


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
