"""Channel mix params. The extremes are the risk: 0 is a legal value."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fl_mcp.flp_writer import set_channel_levels

STOCK = Path(
    "/Applications/FL Studio 2025.app/Contents/Resources/FL/Data/Templates/"
    "Utility/Create a chord progression/Create a chord progression.flp"
)
pytestmark = pytest.mark.skipif(not STOCK.is_file(), reason="FL Studio not installed")


@pytest.fixture
def project(tmp_path):
    dest = tmp_path / "p.flp"
    shutil.copy2(STOCK, dest)
    return dest


@pytest.mark.parametrize("pan", [-1.0, -0.5, 0.0, 0.5, 1.0])
def test_pan_round_trips_including_hard_left(project, pan):
    """Hard-left pan is raw 0. Reading it back with `or` reported it as centre."""
    assert set_channel_levels(project, 0, pan=pan)["pan"] == pytest.approx(pan, abs=0.01)


@pytest.mark.parametrize("volume", [0.0, 0.25, 0.781, 1.0])
def test_volume_round_trips_including_silence(project, volume):
    got = set_channel_levels(project, 0, volume=volume)["volume"]
    assert got == pytest.approx(volume, abs=0.01)


@pytest.mark.parametrize("semitones", [-24, -12, 0, 12, 24])
def test_pitch_round_trips(project, semitones):
    assert set_channel_levels(project, 0, pitch_semitones=semitones)["pitch_semitones"] == semitones


def test_unset_params_are_left_alone(project):
    """Setting pan must not reset a volume the user already chose."""
    set_channel_levels(project, 0, volume=0.4, pitch_semitones=7)
    after = set_channel_levels(project, 0, pan=-1.0)
    assert after["volume"] == pytest.approx(0.4, abs=0.01)
    assert after["pitch_semitones"] == 7


def test_channels_are_independent(project):
    set_channel_levels(project, 0, volume=0.2)
    other = set_channel_levels(project, 1, volume=0.9)
    assert other["volume"] == pytest.approx(0.9, abs=0.01)
    assert set_channel_levels(project, 0)["volume"] == pytest.approx(0.2, abs=0.01)


@pytest.mark.parametrize("kwargs", [{"volume": 1.5}, {"volume": -0.1}, {"pan": 2.0}, {"pitch_semitones": 100}])
def test_out_of_range_rejected_without_touching_the_file(project, kwargs):
    before = project.read_bytes()
    with pytest.raises(ValueError, match="out of range"):
        set_channel_levels(project, 0, **kwargs)
    assert project.read_bytes() == before


def test_unknown_channel_rejected(project):
    with pytest.raises(IndexError):
        set_channel_levels(project, 99, volume=0.5)
