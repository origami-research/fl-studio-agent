"""Composition helpers — pure math, no FL round trip (docs/11 kernel surface).

``chord``/``scale`` return MIDI note numbers; ``drums``/``transpose``/
``quantize`` work on ``schemas.Note`` lists so they compose directly with
``Track.write`` in kernel.py. Never mutate an input ``Note`` — callers may
still hold the original list (e.g. to write it somewhere else too).
"""

from __future__ import annotations

from . import schemas
from .notes import NoteError, parse_key

CHORD_INTERVALS: dict[str, list[int]] = {
    "major": [0, 4, 7],
    "minor": [0, 3, 7],
    "dim": [0, 3, 6],
    "aug": [0, 4, 8],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "maj7": [0, 4, 7, 11],
    "min7": [0, 3, 7, 10],
    "dom7": [0, 4, 7, 10],
}

SCALE_INTERVALS: dict[str, list[int]] = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
    "chromatic": list(range(12)),
}


def chord(root: str | int, quality: str = "major") -> list[int]:
    """A chord's MIDI notes in root position, e.g. chord("C4", "maj7")."""
    intervals = CHORD_INTERVALS.get(quality)
    if intervals is None:
        raise NoteError(f"unknown chord quality {quality!r}: expected one of {sorted(CHORD_INTERVALS)}")
    root_midi = parse_key(root)
    return [_bounded(root_midi + i) for i in intervals]


def scale(root: str | int, mode: str = "major", octaves: int = 1) -> list[int]:
    """A scale's MIDI notes ascending from root, e.g. scale("C4", "dorian", 2)."""
    intervals = SCALE_INTERVALS.get(mode)
    if intervals is None:
        raise NoteError(f"unknown scale mode {mode!r}: expected one of {sorted(SCALE_INTERVALS)}")
    if octaves < 1:
        raise NoteError(f"scale: octaves must be >= 1, got {octaves}")
    root_midi = parse_key(root)
    return [_bounded(root_midi + i + 12 * o) for o in range(octaves) for i in intervals]


def drums(pattern: str, key: str | int = "C1", step_beats: float = 0.25, velocity: float = 0.8) -> list[schemas.Note]:
    """A step string like "x---x---x---x---" into one Note per non-rest step.

    Any character other than space, '-', or '.' counts as a hit — so "x" and
    "X" both work, and accents can later be encoded as different hit chars
    without changing this parser.
    """
    if step_beats <= 0:
        raise NoteError(f"drums: step_beats must be > 0, got {step_beats}")
    rests = (" ", "-", ".")
    return [
        schemas.Note(key=key, start=i * step_beats, length=step_beats, velocity=velocity)
        for i, ch in enumerate(pattern)
        if ch not in rests
    ]


def transpose(notes: list[schemas.Note], semitones: int) -> list[schemas.Note]:
    """A new list with every note shifted by ``semitones``; the input is untouched."""
    out = []
    for n in notes:
        shifted = n.key + semitones
        if not 0 <= shifted <= 127:
            raise NoteError(f"transpose: note {n.key} + {semitones} = {shifted}, outside 0-127")
        out.append(n.model_copy(update={"key": shifted}))
    return out


def quantize(notes: list[schemas.Note], grid: float = 0.25) -> list[schemas.Note]:
    """A new list with every note's start snapped to the nearest ``grid`` multiple."""
    if grid <= 0:
        raise NoteError(f"quantize: grid must be > 0, got {grid}")
    return [n.model_copy(update={"start": round(n.start / grid) * grid}) for n in notes]


def _bounded(midi: int) -> int:
    if not 0 <= midi <= 127:
        raise NoteError(f"note resolves to MIDI {midi}, outside 0-127")
    return midi
