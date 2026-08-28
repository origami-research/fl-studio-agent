from __future__ import annotations

import pytest

from fl_mcp import theory
from fl_mcp.pitch import NoteError


def test_chord_major_triad():
    assert theory.chord("C4", "major") == [60, 64, 67]


def test_chord_default_quality_is_major():
    assert theory.chord("C4") == theory.chord("C4", "major")


def test_chord_maj7_adds_seventh():
    assert theory.chord("C4", "maj7") == [60, 64, 67, 71]


def test_chord_unknown_quality_raises():
    with pytest.raises(NoteError, match="unknown chord quality"):
        theory.chord("C4", "nope")


def test_chord_accepts_midi_root():
    assert theory.chord(60, "minor") == [60, 63, 67]


def test_scale_major_one_octave():
    assert theory.scale("C4", "major") == [60, 62, 64, 65, 67, 69, 71]


def test_scale_two_octaves_doubles_length():
    one = theory.scale("C4", "major", octaves=1)
    two = theory.scale("C4", "major", octaves=2)
    assert len(two) == 2 * len(one)
    assert two[len(one)] == one[0] + 12


def test_scale_unknown_mode_raises():
    with pytest.raises(NoteError, match="unknown scale mode"):
        theory.scale("C4", "nope")


def test_scale_rejects_zero_octaves():
    with pytest.raises(NoteError):
        theory.scale("C4", octaves=0)


def test_drums_parses_hits_at_step_positions():
    notes = theory.drums("x---x---", step_beats=0.25)
    assert [n.start for n in notes] == [0.0, 1.0]
    assert all(n.length == 0.25 for n in notes)


def test_drums_ignores_dots_and_spaces_as_rests():
    notes = theory.drums("x.x x")
    assert len(notes) == 3


def test_drums_custom_key_and_velocity():
    notes = theory.drums("x", key="C1", velocity=0.5)
    assert notes[0].key == 24  # "C1" resolved to MIDI once, at construction
    assert notes[0].velocity == 0.5


def test_drums_rejects_non_positive_step():
    with pytest.raises(NoteError):
        theory.drums("x", step_beats=0)


def test_transpose_shifts_without_mutating_input():
    from fl_mcp import schemas

    original = [schemas.Note(key="C4", start=0, length=1)]
    shifted = theory.transpose(original, 12)

    assert shifted[0].key == 72
    assert original[0].key == 60  # input untouched (C4 resolved to MIDI at construction)


def test_transpose_out_of_range_raises():
    from fl_mcp import schemas

    notes = [schemas.Note(key=125, start=0, length=1)]
    with pytest.raises(NoteError, match="outside 0-127"):
        theory.transpose(notes, 10)


def test_quantize_snaps_start_to_grid():
    from fl_mcp import schemas

    notes = [schemas.Note(key="C4", start=0.9, length=1)]
    quantized = theory.quantize(notes, grid=0.5)

    assert quantized[0].start == 1.0
    assert notes[0].start == 0.9  # input untouched


def test_quantize_rejects_non_positive_grid():
    from fl_mcp import schemas

    with pytest.raises(NoteError):
        theory.quantize([schemas.Note(key="C4", start=0, length=1)], grid=0)
