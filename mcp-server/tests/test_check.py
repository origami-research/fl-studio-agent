"""fl.check(): which channels actually sounded, from FL's own mixer meters.

The real FLSession, link and check run against a scripted FL controller whose
meters (``mixer.getTrackPeaks``) report simulated peaks while "playing". The
rules held here: a silent channel is reported silent with the reason FL shows,
a level is never claimed for a channel that cannot be told apart, FL is left
stopped, and no mute or solo state is touched. What the meters read on real FL
is the live gate (tests/e2e), not this file.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from session_fakes import HELLO, FakeControl, isolate_spool, make_session

from fl_mcp import errors
from fl_mcp.schemas import ControlResponse, Note, Target
from fl_mcp.session.check import METER_DECAY_SECONDS, _overlaps_looped
from fl_mcp.template import template_path


@pytest.fixture(autouse=True)
def isolated_spool(monkeypatch):
    isolate_spool(monkeypatch)


class FakeClock:
    """Time that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# idx, name, mixer track, muted
CHANNELS = [
    (0, "Kick", 1, False),
    (1, "Clap", 2, True),
    (2, "HiHat", 3, False),
    (3, "Snare", 4, False),
    (4, "Bass", 5, False),
    (5, "Sub", 5, False),
    (6, "Pad", 0, False),
]
MIXER = ["Master", "Kick", "Clap", "HiHat", "Snare", "Low end"]


class PlayingFL(FakeControl):
    """FL's controller with transport state and meters that read only while playing."""

    def __init__(self, peaks: dict[int, list[float]], *, loop_mode: int = 0, playing: bool = False,
                 song_last_tick: int = 1535, tempo_bpm: float = 120.0) -> None:
        self.playing = playing
        self.reads: dict[int, int] = {}
        super().__init__(
            hello=HELLO,
            handlers={
                "state.snapshot": lambda args: self._snapshot(tempo_bpm),
                "transport.getLoopMode": lambda args: loop_mode,
                "patterns.getPatternLength": lambda args: 64,  # 16th steps: 4 bars
                "transport.getSongLength": lambda args: song_last_tick,
                "transport.setSongPos": lambda args: None,
                "transport.start": lambda args: self._play(True),
                "transport.stop": lambda args: self._play(False),
                "mixer.getTrackPeaks": lambda args: self._peak(peaks, args),
            },
        )

    def _snapshot(self, tempo_bpm: float) -> dict[str, Any]:
        return {
            "tempo": round(tempo_bpm * 1000),
            "ppq": 96,
            "playing": self.playing,
            "song_pos": 0.0,
            "current_pattern": 1,
            "pattern_count": 1,
            "channels": [
                {"idx": i, "name": n, "muted": m, "vol": 0.78, "pan": 0.0, "target_mixer_track": t}
                for i, n, t, m in CHANNELS
            ],
            "mixer": [
                {"idx": i, "name": n, "vol": 0.8, "pan": 0.0, "muted": False, "solo": False}
                for i, n in enumerate(MIXER)
            ],
        }

    def _play(self, playing: bool) -> None:
        self.playing = playing

    def _peak(self, peaks: dict[int, list[float]], args: list[Any]) -> float:
        track, mode = args
        assert mode == 2, "the louder of left and right"
        if not self.playing:
            return 0.0
        n = self.reads.get(track, 0)
        self.reads[track] = n + 1
        series = peaks.get(track, [0.0])
        return series[n % len(series)]

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


@pytest.fixture
def song(tmp_path) -> Path:
    """The song FL opened: the template (no notes; pattern 1 on the playlist's first four bars)."""
    project = tmp_path / "agent.flp"
    shutil.copyfile(template_path(), project)
    return project


def seeded(control: PlayingFL, clock: FakeClock, project: Path | None):
    """A session whose note record holds what each channel was written."""
    session, store = make_session(control=control, monotonic=clock, sleep=clock.sleep, project=project)
    beat = lambda b: [Note(key=60, start=b, length=1)]  # noqa: E731 - one expression
    for channel in (0, 1, 3, 4, 5, 6):
        store.record_write(Target(pattern=1, channel=channel), [], beat(0), "replace", "bridge", channel + 1)
    store.record_write(Target(pattern=1, channel=2), [], beat(12), "replace", "bridge", 9)  # bar 4 only
    return session


def by_name(result) -> dict[str, Any]:
    return {c.name: c for c in result.channels}


def test_check_reports_each_channel_with_the_reason_it_was_silent(song):
    # Kick's meter peaks once at 0.5 (-6 dBFS) between quieter reads: the
    # loudest reading is kept. Track 4 (Snare) never moves.
    control = PlayingFL({0: [0.9], 1: [0.1, 0.5, 0.2], 5: [0.25], 2: [0.3]})
    clock = FakeClock()
    result = seeded(control, clock, song).check(1, 2)

    assert (result.start_bar, result.bars, result.seconds) == (1, 2, 4.0)
    assert result.master_peak_dbfs == -0.9
    ch = by_name(result)
    assert (ch["Kick"].sounded, ch["Kick"].peak_dbfs, ch["Kick"].reason) == (True, -6.0, None)
    # Muted in FL: silent, even though something sounded on its mixer track.
    assert (ch["Clap"].sounded, ch["Clap"].peak_dbfs, ch["Clap"].reason) == (False, None, "it is muted")
    assert (ch["HiHat"].sounded, ch["HiHat"].reason) == (False, "it has no notes in bars 1-2")
    assert (ch["Snare"].sounded, ch["Snare"].reason) == (False, "it made no sound; check its instrument and levels")
    # Two channels on one mixer track are measured together, and say so.
    assert ch["Bass"].sounded is True and ch["Bass"].reason == "measured together with Sub on mixer track 5"
    assert ch["Sub"].peak_dbfs == -12.0
    # Straight into the master: not measurable on its own, never guessed.
    assert ch["Pad"].sounded is None and ch["Pad"].peak_dbfs is None
    assert "straight into the master" in ch["Pad"].reason
    assert result.summary.startswith("3 of 7 channels sounded in bars 1-2; the master peaked at -0.9 dBFS.")
    assert "Snare (it made no sound; check its instrument and levels)" in result.summary


def test_check_plays_from_the_bar_asked_and_leaves_fl_stopped_with_mutes_untouched(song):
    control = PlayingFL({1: [0.5]})
    clock = FakeClock()
    seeded(control, clock, song).check(3, 1)

    ops = control.ops()
    assert ("transport.setSongPos", [2 * 4 * 96, 2]) in control.calls  # bar 3 in absolute ticks
    assert ops.index("transport.setSongPos") < ops.index("transport.start") < ops.index("mixer.getTrackPeaks")
    assert ops[-1] == "transport.stop" and control.playing is False
    assert not [op for op in ops if "mute" in op.lower() or "solo" in op.lower()]
    # Earlier meters settle first, then one bar at 120 BPM: two seconds of polling.
    assert clock.sleeps[0] == METER_DECAY_SECONDS
    assert 1.95 <= sum(clock.sleeps[1:]) < 2.2


def test_check_stops_a_playing_song_first_and_lets_its_meters_fall(song):
    control = PlayingFL({1: [0.5]}, playing=True)
    clock = FakeClock()
    seeded(control, clock, song).check(1, 1)

    ops = control.ops()
    assert ops.index("transport.stop") < ops.index("transport.setSongPos")
    assert control.playing is False


def test_check_stops_fl_even_when_a_meter_read_fails(song):
    control = PlayingFL({})
    control.handlers["mixer.getTrackPeaks"] = lambda args: ControlResponse(id=0, ok=False, err="TypeError: bad")
    clock = FakeClock()
    with pytest.raises(errors.ToolError) as excinfo:
        seeded(control, clock, song).check(1, 2)

    assert excinfo.value.category == "fl_api_error"
    assert control.ops()[-1] == "transport.stop" and control.playing is False


@pytest.mark.parametrize(("start_bar", "bars"), [(0, 2), (1, 0), (1, 9), (True, 2)])
def test_check_refuses_bad_spans_before_playing(start_bar, bars, song):
    control = PlayingFL({})
    with pytest.raises(errors.ToolError) as excinfo:
        seeded(control, FakeClock(), song).check(start_bar, bars)
    assert excinfo.value.category == "invalid_argument"
    assert "transport.start" not in control.ops()


def test_check_past_the_end_of_the_pattern_names_its_length(song):
    control = PlayingFL({})
    with pytest.raises(errors.ToolError) as excinfo:
        seeded(control, FakeClock(), song).check(5, 1)
    assert "pattern 1 is 4 bars long; start at bar 1 to 4" in excinfo.value.message
    assert "transport.start" not in control.ops()


def test_check_in_song_mode_with_an_empty_playlist_says_nothing_would_play(song):
    control = PlayingFL({}, loop_mode=1, song_last_tick=0)
    with pytest.raises(errors.ToolError) as excinfo:
        seeded(control, FakeClock(), song).check(1, 2)
    assert excinfo.value.message == "FL is in song mode and its playlist is empty, so nothing would play."
    assert "transport.start" not in control.ops()


def test_check_in_song_mode_reads_which_patterns_play_from_the_playlist(song):
    """The template's playlist holds pattern 1 for its first four bars, so bars
    5-6 play nothing on the playlist: every silent channel says so."""
    control = PlayingFL({}, loop_mode=1, song_last_tick=3071)
    result = seeded(control, FakeClock(), song).check(5, 2)

    assert {c.reason for c in result.channels if c.name != "Clap"} == {"it has no notes in bars 5-6"}
    assert result.summary.startswith("0 of 7 channels sounded in bars 5-6; the master stayed silent.")


def test_a_check_that_hears_tracks_but_a_silent_master_says_nothing_reaches_the_mix(song):
    control = PlayingFL({1: [0.5]})
    result = seeded(control, FakeClock(), song).check(1, 1)
    assert by_name(result)["Kick"].sounded is True
    assert result.master_peak_dbfs is None
    assert "nothing reaches the master" in result.summary


def test_without_the_song_fl_opened_no_channel_is_called_silent_on_the_note_record():
    """The note record is only half the picture without the song FL opened, so
    the meters alone decide: a sounding shared track is not pinned on anyone."""
    control = PlayingFL({5: [0.25]})
    ch = by_name(seeded(control, FakeClock(), None).check(1, 2))
    assert ch["HiHat"].reason == "it made no sound; check its instrument and levels"
    assert ch["Bass"].sounded is True and ch["Sub"].sounded is True


def test_a_note_in_a_looping_pattern_counts_on_every_pass():
    note = Note(key=60, start=3, length=0.5)  # the last beat of a one-bar pattern
    assert _overlaps_looped(note, start=4, end=8, length=4)  # second pass
    assert not _overlaps_looped(note, start=0, end=3, length=4)
    held = Note(key=60, start=3, length=2)  # rings into the next bar
    assert _overlaps_looped(held, start=4, end=4.5, length=8)
