from __future__ import annotations

import pytest

from fl_mcp.notes import NoteError, describe, key_name, normalize_notes, parse_key


@pytest.mark.parametrize(
    ("name", "midi"),
    [
        ("C4", 60),
        ("c4", 60),
        ("C#4", 61),
        ("Db4", 61),
        ("B3", 59),
        ("C5", 72),
        ("A0", 21),
        ("G9", 127),
        ("C-1", 0),
        ("Bb5", 82),
    ],
)
def test_note_names_resolve_to_midi(name, midi):
    assert parse_key(name) == midi


def test_midi_numbers_pass_through():
    assert parse_key(60) == 60
    assert parse_key(0) == 0
    assert parse_key(127) == 127


def test_key_name_round_trips():
    for midi in range(0, 128):
        assert parse_key(key_name(midi)) == midi


@pytest.mark.parametrize(
    "bad",
    ["H4", "C", "", "  ", "C#x", 128, -1, 60.5, True, None, ["C4"]],
)
def test_invalid_keys_rejected(bad):
    with pytest.raises(NoteError):
        parse_key(bad)


def test_defaults_are_filled_in():
    (note,) = normalize_notes([{"key": "C4", "start": 0, "length": 1}])
    assert note == {
        "key": 60,
        "start": 0.0,
        "length": 1.0,
        "velocity": 0.8,
        "pan": 0.5,
        "color": 0,
    }


def test_missing_required_fields_named_in_error():
    with pytest.raises(NoteError, match="start, length"):
        normalize_notes([{"key": "C4"}])


def test_zero_length_rejected():
    with pytest.raises(NoteError, match="greater than 0"):
        normalize_notes([{"key": "C4", "start": 0, "length": 0}])


def test_out_of_range_velocity_rejected():
    with pytest.raises(NoteError, match="velocity"):
        normalize_notes([{"key": "C4", "start": 0, "length": 1, "velocity": 1.5}])


def test_empty_and_oversized_lists_rejected():
    with pytest.raises(NoteError, match="empty"):
        normalize_notes([])
    with pytest.raises(NoteError, match="too many"):
        normalize_notes([{"key": 60, "start": 0, "length": 1}] * 5001)


def test_error_message_identifies_which_note():
    with pytest.raises(NoteError, match="note 2"):
        normalize_notes(
            [
                {"key": "C4", "start": 0, "length": 1},
                {"key": "D4", "start": 1, "length": 1},
                {"key": "nope", "start": 2, "length": 1},
            ]
        )


def test_describe_summarizes():
    notes = normalize_notes(
        [{"key": "C4", "start": 0, "length": 1}, {"key": "E4", "start": 1, "length": 2}]
    )
    assert describe(notes) == "2 notes over 3 beats (C4, E4)"
