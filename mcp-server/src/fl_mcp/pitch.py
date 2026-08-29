"""Pitch conversion: note name <-> MIDI number. The single source of truth.

A leaf module with no internal dependencies, so both ``schemas`` (which firms
``Note.key`` to an int at construction) and ``notes`` (which reports per-note
error context) import it without a cycle. There is exactly one name->MIDI
parser in the codebase; it lives here.

Scientific pitch notation: C4 == MIDI 60 == middle C. FL Studio's piano roll
*labels* MIDI 60 as "C5" (its display uses a +1 octave offset); we follow the
MIDI standard. If an octave lands wrong, pass a raw MIDI integer instead.
"""

from __future__ import annotations

from typing import Any

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
