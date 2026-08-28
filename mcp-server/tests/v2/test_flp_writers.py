"""Lane F writer tests: raw byte surgery, verified by re-reading the file.

set_tempo is a PORT of the live-verified v1 (flp_writer.set_tempo, commit
13f8afc): the byte-differential test below asserts both produce identical
files, so any byte-level drift in the port fails loudly.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

from fl_mcp import flp_writer
from fl_mcp.flpaths import fl_app
from fl_mcp.v2 import flp
from fl_mcp.v2.schemas import Note

from .test_flp_reader import VERSION_MODERN, build_flp, event, note_record, write_flp

_FL_DATA = fl_app() / "Contents/Resources/FL/Data"

requires_fl = pytest.mark.skipif(
    not _FL_DATA.is_dir(), reason="FL Studio is not installed on this machine"
)

# pyflp renders keys as sharp note names with C5 = MIDI 60 (its octaves run
# C0=0 .. B10=131); ours are firm MIDI ints. One inverse, used by every
# oracle comparison.
_PYFLP_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def pyflp_key_to_midi(key: str) -> int:
    letter = key.rstrip("0123456789")
    return _PYFLP_NOTE_NAMES.index(letter) + 12 * int(key[len(letter) :])


# -- set_tempo ----------------------------------------------------------------


def test_set_tempo_patches_existing_event_in_place(tmp_path):
    events = (
        VERSION_MODERN
        + event(flp.EVENT_TEMPO, struct.pack("<I", 140_000))
        + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", 0))
    )
    path = write_flp(tmp_path, events)
    before = path.read_bytes()

    assert flp.set_tempo(path, 137) == 137.0

    after = path.read_bytes()
    assert len(after) == len(before)  # in-place patch, no splice
    assert flp.read(path).tempo == 137.0


def test_set_tempo_appends_at_end_of_stream_when_absent(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    before = path.read_bytes()

    assert flp.set_tempo(path, 133) == 133.0

    after = path.read_bytes()
    # The event lands literally at the end of the stream, and the FLdt length
    # grows by exactly the 5 appended bytes - the live-verified v1 decision.
    assert after[-5:] == bytes([flp.EVENT_TEMPO]) + struct.pack("<I", 133_000)
    assert len(after) == len(before) + 5
    assert flp.read(path).tempo == 133.0


def test_set_tempo_rejects_out_of_range():
    for bpm in (9.9, 999.1, 0, -10):
        with pytest.raises(ValueError, match="out of range"):
            flp.set_tempo(Path("/nonexistent.flp"), bpm)


def test_set_tempo_readback_is_the_stored_quantized_value(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    stored = flp.set_tempo(path, 128.5004)
    assert stored == round(128.5004 * 1000) / 1000


def test_set_tempo_byte_identical_to_v1_port_source(tmp_path):
    """Both implementations must write the same bytes - the port contract."""
    events = VERSION_MODERN + event(flp.EVENT_TEMPO, struct.pack("<I", 140_000))
    for name, payload in (("with_tempo", events), ("no_tempo", VERSION_MODERN)):
        v1_path = tmp_path / f"{name}_v1.flp"
        v2_path = tmp_path / f"{name}_v2.flp"
        v1_path.write_bytes(build_flp(payload))
        v2_path.write_bytes(build_flp(payload))

        flp_writer.set_tempo(v1_path, 117)
        flp.set_tempo(v2_path, 117)

        assert v1_path.read_bytes() == v2_path.read_bytes()


# -- write_notes --------------------------------------------------------------


def pattern_events(number: int, *blobs: bytes) -> bytes:
    out = event(flp.EVENT_PATTERN_NEW, struct.pack("<H", number))
    for blob in blobs:
        out += event(flp.EVENT_PATTERN_NOTES, blob)
    return out


def test_write_notes_creates_pattern_when_file_has_none(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    notes = [Note(key=60, start=0.0, length=1.0), Note(key=64, start=1.0, length=0.5)]

    result = flp.write_notes(path, notes, pattern=1, channel=0, mode="replace")

    assert [(n.key, n.position, n.length) for n in result] == [(60, 0, 96), (64, 96, 48)]
    # The result IS notes_at of the saved file, not an echo.
    assert result == flp.notes_at(path, 1, 0)


def test_write_notes_creates_pattern_after_cur_group_anchor(tmp_path):
    anchor = event(flp.EVENT_CUR_GROUP_ID, struct.pack("<i", -1))
    path = write_flp(tmp_path, VERSION_MODERN + anchor)

    flp.write_notes(path, [Note(key=60, start=0.0, length=1.0)], pattern=2, channel=0, mode="merge")

    data = path.read_bytes()
    # PatternID.New lands immediately after the anchor event, 224 after it -
    # v1's placement, which real FL accepts.
    anchor_at = data.index(bytes([flp.EVENT_CUR_GROUP_ID]))
    assert data[anchor_at + 5] == flp.EVENT_PATTERN_NEW
    assert flp.read(path).notes_in(2)[0].key == 60


def test_write_notes_merge_keeps_existing_notes(tmp_path):
    existing = note_record(position=0, key=48, channel=0)
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, existing))

    result = flp.write_notes(
        path, [Note(key=72, start=2.0, length=1.0)], pattern=1, channel=0, mode="merge"
    )

    assert sorted(n.key for n in result) == [48, 72]


def test_write_notes_replace_drops_existing_notes(tmp_path):
    existing = note_record(position=0, key=48) + note_record(position=24, key=50)
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, existing))

    result = flp.write_notes(
        path, [Note(key=72, start=0.0, length=1.0)], pattern=1, channel=0, mode="replace"
    )

    assert [n.key for n in result] == [72]
    assert flp.notes_at(path, 1, 0) == result


def test_write_notes_inserts_after_pattern_event_when_no_notes_yet(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(3))

    flp.write_notes(path, [Note(key=61, start=0.0, length=1.0)], pattern=3, channel=2, mode="merge")

    (note,) = flp.notes_at(path, 3, 2)
    assert (note.key, note.channel, note.pattern) == (61, 2, 3)


def test_write_notes_targets_only_the_requested_pattern(tmp_path):
    p1 = pattern_events(1, note_record(position=0, key=40))
    p2 = pattern_events(2, note_record(position=0, key=41))
    path = write_flp(tmp_path, VERSION_MODERN + p1 + p2)

    flp.write_notes(path, [Note(key=90, start=0.0, length=1.0)], pattern=2, channel=0, mode="merge")

    project = flp.read(path)
    assert [n.key for n in project.notes_in(1)] == [40]  # untouched
    assert sorted(n.key for n in project.notes_in(2)) == [41, 90]


def test_write_notes_does_not_adopt_another_patterns_blob(tmp_path):
    # Pattern 1 exists with no notes event; pattern 2 has one. v1's scan would
    # have merged into pattern 2's blob; the fixed attribution must not.
    p1 = pattern_events(1)
    p2 = pattern_events(2, note_record(position=0, key=41))
    path = write_flp(tmp_path, VERSION_MODERN + p1 + p2)

    flp.write_notes(path, [Note(key=90, start=0.0, length=1.0)], pattern=1, channel=0, mode="merge")

    project = flp.read(path)
    assert [n.key for n in project.notes_in(1)] == [90]
    assert [n.key for n in project.notes_in(2)] == [41]


def test_write_notes_uses_file_ppq_for_tick_quantization(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN, ppq=192)

    result = flp.write_notes(
        path, [Note(key=60, start=1.5, length=0.25)], pattern=1, channel=0, mode="replace"
    )

    (note,) = result
    assert (note.position, note.length) == (288, 48)


def test_write_notes_velocity_and_pan_quantization(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)

    (note,) = flp.write_notes(
        path,
        [Note(key=60, start=0.0, length=1.0, velocity=1.0, pan=0.0)],
        pattern=1, channel=0, mode="replace",
    )

    assert (note.velocity, note.pan) == (127, 0)


def test_write_notes_empty_replace_clears_the_pattern(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN + pattern_events(1, note_record(key=48)))

    result = flp.write_notes(path, [], pattern=1, channel=0, mode="replace")

    assert result == []
    assert flp.read(path).notes_in(1) == []


def test_write_notes_result_still_a_valid_flp(tmp_path):
    # A large blob forces a multi-byte varint length; the file must re-parse.
    path = write_flp(tmp_path, VERSION_MODERN)
    notes = [Note(key=36 + (i % 48), start=i * 0.25, length=0.25) for i in range(64)]

    result = flp.write_notes(path, notes, pattern=1, channel=0, mode="replace")

    assert len(result) == 64
    assert len(flp.read(path).notes_in(1)) == 64


@requires_fl
def test_write_notes_into_stock_template_pyflp_agrees(tmp_path):
    """pyflp (the dev-only oracle) must read back exactly what we spliced."""
    from harness.flp_compat import parse

    path = tmp_path / "empty.flp"
    shutil.copy(_FL_DATA / "Templates/Minimal/Empty/Empty.flp", path)
    notes = [Note(key=60, start=0.0, length=1.0), Note(key=67, start=1.0, length=1.0)]

    result = flp.write_notes(path, notes, pattern=1, channel=0, mode="replace")
    assert [(n.key, n.position) for n in result] == [(60, 0), (67, 96)]

    oracle = parse(path)
    oracle_notes = [(pyflp_key_to_midi(n.key), n.position) for p in oracle.patterns for n in p.notes]
    ours = [(n.key, n.position) for n in flp.read(path).notes]
    assert oracle_notes == ours


@requires_fl
def test_set_tempo_on_stock_templates_both_paths(tmp_path):
    # Empty.flp carries a tempo event (patch path); Basic with limiter omits
    # it (append path) - the same pair the live e2e uses.
    cases = [
        ("Templates/Minimal/Empty/Empty.flp", "patch"),
        ("Templates/Minimal/Basic with limiter/Basic with limiter.flp", "append"),
    ]
    for rel, label in cases:
        path = tmp_path / f"{label}.flp"
        shutil.copy(_FL_DATA / rel, path)
        grew = len(path.read_bytes())

        assert flp.set_tempo(path, 123) == 123.0

        expected_growth = 0 if label == "patch" else 5
        assert len(path.read_bytes()) == grew + expected_growth
