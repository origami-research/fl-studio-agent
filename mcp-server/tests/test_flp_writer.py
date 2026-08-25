"""Tests for the autonomous backend: notes written straight into a .flp.

Every assertion reads the notes back out of the saved file rather than trusting
the writer's return value — the whole point of this backend is that the file is
the ground truth FL will load.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from fl_mcp.flp_writer import NOTE_SIZE, PPQ, write_notes  # noqa: E402
from harness.verify import read_notes  # noqa: E402

TEMPLATE = REPO / "harness" / "projects" / "fl_mcp_bridge.flp"

pytestmark = pytest.mark.skipif(
    not TEMPLATE.is_file(),
    reason="needs a generated base project (python -m harness.setup_fl)",
)


@pytest.fixture
def project(tmp_path):
    dest = tmp_path / "agent.flp"
    shutil.copy2(TEMPLATE, dest)
    return dest


def midis(path):
    return [r.midi for r in read_notes(path)]


def test_notes_land_in_the_file(project):
    write_notes(
        project,
        [
            {"key": 64, "start": 0, "length": 0.5},
            {"key": 62, "start": 0.5, "length": 0.5},
            {"key": 60, "start": 1, "length": 1},
        ],
        mode="replace",
    )
    rows = read_notes(project)
    assert [r.midi for r in rows] == [64, 62, 60]
    assert [r.start_beat for r in rows] == [0.0, 0.5, 1.0]
    assert [r.length_beats for r in rows] == [0.5, 0.5, 1.0]


def test_beats_convert_to_ticks(project):
    write_notes(project, [{"key": 60, "start": 2, "length": 1.5}], mode="replace")
    row = read_notes(project)[0]
    assert row.position == 2 * PPQ
    assert row.length == int(1.5 * PPQ)


def test_merge_accumulates_replace_resets(project):
    base = [{"key": 60, "start": 0, "length": 1}]
    write_notes(project, base, mode="replace")
    assert len(read_notes(project)) == 1

    write_notes(project, [{"key": 67, "start": 1, "length": 1}], mode="merge")
    assert sorted(midis(project)) == [60, 67], "merge must keep existing notes"

    write_notes(project, base, mode="replace")
    assert midis(project) == [60], "replace must drop existing notes"


def test_repeated_writes_do_not_accumulate_events(project):
    """Regression: rebuilding the notes event must replace it, not stack copies."""
    for _ in range(5):
        write_notes(project, [{"key": 60, "start": 0, "length": 1}], mode="replace")
    assert len(read_notes(project)) == 1


def test_channel_is_recorded(project):
    write_notes(project, [{"key": 60, "start": 0, "length": 1}], channel=3, mode="replace")
    assert read_notes(project)[0].channel == 3


def test_velocity_scales_from_unit_range(project):
    write_notes(
        project,
        [
            {"key": 60, "start": 0, "length": 1, "velocity": 1.0},
            {"key": 62, "start": 1, "length": 1, "velocity": 0.5},
        ],
        mode="replace",
    )
    rows = read_notes(project)
    assert rows[0].velocity == 127
    assert 60 <= rows[1].velocity <= 68  # ~half, allowing for rounding


def test_empty_replace_clears(project):
    write_notes(project, [{"key": 60, "start": 0, "length": 1}], mode="replace")
    write_notes(project, [], mode="replace")
    assert read_notes(project) == []


def test_large_write_round_trips(project):
    notes = [{"key": 48 + (i % 24), "start": i * 0.25, "length": 0.25} for i in range(200)]
    write_notes(project, notes, mode="replace")
    rows = read_notes(project)
    assert len(rows) == 200
    assert rows[-1].start_beat == pytest.approx(199 * 0.25)


def test_note_struct_is_24_bytes():
    """The file format is fixed; a mismatch would silently corrupt every note."""
    assert NOTE_SIZE == 24
