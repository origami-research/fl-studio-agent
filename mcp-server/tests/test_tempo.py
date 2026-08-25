"""Tempo writing: the units are the whole risk here."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fl_mcp.flp_writer import set_tempo

STOCK = Path(
    "/Applications/FL Studio 2025.app/Contents/Resources/FL/Data/Templates/"
    "Minimal/Basic with limiter/Basic with limiter.flp"
)

pytestmark = pytest.mark.skipif(not STOCK.is_file(), reason="FL Studio not installed")


@pytest.fixture
def project(tmp_path):
    dest = tmp_path / "p.flp"
    shutil.copy2(STOCK, dest)
    return dest


def _tempo(path):
    from harness.flp_compat import parse
    return parse(path).tempo


@pytest.mark.parametrize("bpm", [90, 128, 130.0, 174, 128.5])
def test_round_trips(project, bpm):
    """FL stores BPM x 1000; writing the float would give 0.13 BPM."""
    assert set_tempo(project, bpm) == pytest.approx(bpm)
    assert _tempo(project) == pytest.approx(bpm)


def test_survives_a_note_write(project):
    """Tempo and notes are separate events; one must not clobber the other."""
    from fl_mcp.flp_writer import write_notes

    set_tempo(project, 174)
    write_notes(project, [{"key": 60, "start": 0, "length": 1}], channel=0)
    assert _tempo(project) == pytest.approx(174)


@pytest.mark.parametrize("bad", [0, 5, 1000, -120])
def test_rejects_out_of_range(project, bad):
    before = _tempo(project)
    with pytest.raises(ValueError, match="out of range"):
        set_tempo(project, bad)
    assert _tempo(project) == before, "a rejected tempo must not modify the file"
