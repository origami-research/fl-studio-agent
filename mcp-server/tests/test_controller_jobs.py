"""The controller's recording jobs (ctl.record, ctl.job), stepped on a fake FL.

The fake answers as FL 2026 did live (docs/research/arrangement-automation-live-2026-11-09.md):
recording arms a tick after record() is called, the song position stops at the song's end
(a time marker extends it), getSongPos stays at that end while currentTime follows the
playhead, and a value is recorded only while FL records and plays. Each test names the rule
it holds: a job never leaves FL recording, records only inside its window, and fails with
a reason rather than waiting forever.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fl_mcp.fl_scripts import device_FLMCP as flmcp

PPQ = 96
BAR = 4 * PPQ
MIDI = SimpleNamespace(
    SONGLENGTH_ABSTICKS=2,
    REC_Chan_Vol=0,
    REC_Chan_Pan=1,
    REC_Chan_Plugin_First=0x8000,
    REC_Mixer_Vol=0x20000000 + 0x2000 + 0x40 + 0x81,
    REC_Mixer_Pan=0x20000000 + 0x2000 + 0x40 + 0x82,
    REC_Plug_MixLevel=0x20000000 + 0x2000 + 1,
    REC_Plug_Plugin_First=0x20000000 + 0x8000,
    FromMIDI_Max=1 << 30,
    REC_MIDIController=0x40 | 0x1 | 0x20,
)


class FakeFL:
    """FL's transport, song and controls as the controller sees them."""

    def __init__(self, song_ticks: int = 4 * BAR, *, arms: bool = True, step: int = 12, loops: bool = False) -> None:
        self.song_ticks = song_ticks
        self.loops = loops  # playing past the song's end goes back to its start, as FL's song mode does
        self.loop_mode = 0
        self.pos = 0
        self.playing = False
        self.armed = False
        self.arm_pending = False
        self.arms = arms
        self.step = step
        self.pattern = 1
        self.markers: list[tuple[int, str]] = []
        self.recorded: list[tuple[int, int, int]] = []  # (tick, event, value) kept in the pattern
        self.values: dict[int, float] = {}
        self.clock = 0.0

    def tick(self) -> None:
        """One OnIdle: a pending arm lands, the playhead moves, then the job steps."""
        self.clock += 0.02
        if self.arm_pending:
            self.arm_pending = False
            if self.arms:
                self.armed = not self.armed
        if self.playing:
            self.pos += self.step
            if self.loops and self.pos >= self.song_ticks:
                self.pos -= self.song_ticks
        flmcp._job_tick()

    def set_song_pos(self, pos, mode):
        assert mode == MIDI.SONGLENGTH_ABSTICKS
        self.pos = min(pos, self.song_ticks + 1)

    def rec_event(self, event, value, flags):
        self.values[event] = value / MIDI.FromMIDI_Max
        if self.armed and self.playing:
            self.recorded.append((self.pos, event, value))

    def install(self, monkeypatch) -> None:
        fl = self

        def toggle_loop():
            fl.loop_mode = 1 - fl.loop_mode

        def record():
            fl.arm_pending = True

        def add_marker(time, name):
            fl.markers.append((time, name))
            fl.song_ticks = max(fl.song_ticks, time - 1)

        patch = {
            "transport": SimpleNamespace(
                getLoopMode=lambda: fl.loop_mode, setLoopMode=toggle_loop, setSongPos=fl.set_song_pos,
                getSongLength=lambda mode: fl.song_ticks, record=record, isRecording=lambda: fl.armed,
                start=lambda: setattr(fl, "playing", True), stop=lambda: setattr(fl, "playing", False),
                isPlaying=lambda: fl.playing,
            ),
            "arrangement": SimpleNamespace(currentTime=lambda snap: fl.pos, addAutoTimeMarker=add_marker),
            "patterns": SimpleNamespace(jumpToPattern=lambda n: setattr(fl, "pattern", n)),
            "general": SimpleNamespace(processRECEvent=fl.rec_event),
            "device": SimpleNamespace(getLinkedValue=lambda event: fl.values.get(event, 1.0)),
            "channels": SimpleNamespace(getRecEventId=lambda ch, useGlobal: ch << 16),
            "mixer": SimpleNamespace(
                getTrackPluginId=lambda track, slot: ((track << 6) + slot) << 16, trackCount=lambda: 3,
                isTrackPluginValid=lambda track, slot: (track, slot) == (0, 9),
            ),
            "plugins": SimpleNamespace(getPluginName=lambda track, slot: "FL MCP Bridge"),
            "playlist": SimpleNamespace(trackCount=lambda: 4, getTrackActivityLevel=lambda t: 0.5 if t == 2 else 0.0),
            "midi": MIDI,
        }
        for name, module in patch.items():
            monkeypatch.setattr(flmcp, name, module, raising=False)
        monkeypatch.setattr(flmcp, "_now", lambda: fl.clock)
        monkeypatch.setattr(flmcp, "_jobs", {"current": None, "last": None, "seq": 0})


def run(fl: FakeFL, spec: dict, ticks: int = 2000) -> dict:
    job = flmcp._start_job([spec], {})["job"]
    for _ in range(ticks):
        fl.tick()
        status = flmcp._job_status([], {})
        if status["id"] == job and status["phase"] in ("done", "failed"):
            return status
    raise AssertionError(f"job still {flmcp._job_status([], {})['phase']}")


def ramp(start_bar: int, bars: int) -> dict:
    return {
        "mode": "record", "pattern": 7, "start": (start_bar - 1) * BAR, "length": bars * BAR, "seconds": 2.0 * bars,
        "target": {"kind": "channel", "channel": 3, "param": "volume"}, "points": [[0, 0.0], [bars * BAR, 1.0]],
        "marker": "Sweep",
    }


def test_a_playback_from_bar_1_to_the_songs_end_stops_when_fl_goes_back_to_the_start(monkeypatch):
    """From bar 1, FL going back to the song's start is not a position before the start,
    so a take over the whole song played on until its window ran out and failed
    (fl-mcp-03, 2026-11-10). Any step backwards ends the take."""
    fl = FakeFL(song_ticks=4 * BAR, loops=True, step=10)
    fl.install(monkeypatch)
    spec = {
        "mode": "play", "start": 0, "length": 4 * BAR, "seconds": 8.0,
        "target": {"kind": "mixer", "track": 1, "param": "volume"},
    }

    status = run(fl, spec)

    assert status["phase"] == "done" and status["error"] is None
    assert status["rows"] and all(0 <= tick < 4 * BAR for tick, _ in status["rows"])
    assert not fl.playing


def test_a_recording_lands_only_inside_its_window_with_the_straight_line_asked_for(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)

    status = run(fl, ramp(9, 2))

    assert status["phase"] == "done" and status["error"] is None
    assert fl.pattern == 7 and fl.loop_mode == 1, "recorded into the pattern, in song mode"
    start, end = 8 * BAR, 10 * BAR
    assert fl.recorded and all(start <= tick < end for tick, _, _ in fl.recorded)
    for tick, event, value in fl.recorded:
        assert event == (3 << 16) + MIDI.REC_Chan_Vol
        assert abs(value / MIDI.FromMIDI_Max - (tick - start) / (2 * BAR)) < 1e-6
    assert status["events"] == len(fl.recorded)
    assert not fl.armed and not fl.playing, "a job never leaves FL recording or playing"


def test_a_start_past_the_songs_end_adds_a_time_marker_so_fl_can_play_from_there(monkeypatch):
    # Live: setSongPos stopped at the song's end on an empty song; a marker extended it.
    fl = FakeFL(song_ticks=BAR - 1)
    fl.install(monkeypatch)

    status = run(fl, ramp(9, 1))

    assert fl.markers == [(8 * BAR, "Sweep")]
    assert status["first"] >= 8 * BAR


def test_playing_back_past_the_songs_end_reads_nothing_and_plays_nothing(monkeypatch):
    fl = FakeFL(song_ticks=BAR - 1)
    fl.install(monkeypatch)

    status = run(fl, {"mode": "play", "start": 8 * BAR, "length": PPQ, "seconds": 0.5, "tracks": True})

    assert status["phase"] == "done" and status["tracks"] == []
    assert fl.markers == [] and fl.pos == 0, "a readback adds nothing to the song"


def test_a_playback_samples_the_control_and_the_active_tracks(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)
    fl.values[(3 << 16)] = 0.25

    status = run(fl, {
        "mode": "play", "start": 4 * BAR, "length": BAR, "seconds": 2.0, "tracks": True,
        "target": {"kind": "channel", "channel": 3, "param": "volume"},
    })

    assert status["tracks"] == [2]
    assert status["rows"] and all(r[1] == 0.25 and 4 * BAR <= r[0] < 5 * BAR for r in status["rows"])
    assert not fl.armed and fl.recorded == [], "a playback records nothing"


def test_a_playback_reports_a_few_samples_once_it_ends(monkeypatch):
    # A status carrying every tick's sample, polled while FL played, preceded FL going
    # silent in a take; the samples are thinned and sent once.
    fl = FakeFL(song_ticks=64 * BAR, step=8)
    fl.install(monkeypatch)
    spec = {"mode": "play", "start": 0, "length": 16 * BAR, "seconds": 30.0,
            "target": {"kind": "channel", "channel": 3, "param": "volume"}}
    job = flmcp._start_job([spec], {})["job"]
    for _ in range(200):
        fl.tick()
    running = flmcp._job_status([], {})
    assert running["id"] == job and running["phase"] == "running" and "rows" not in running

    status = running
    for _ in range(20000):
        fl.tick()
        status = flmcp._job_status([], {})
        if status["phase"] == "done":
            break
    assert status["phase"] == "done"
    assert 0 < len(status["rows"]) <= flmcp.SAMPLE_ROWS_MAX


def test_a_placement_holds_the_inert_mix_level_of_the_effect_that_passes_audio_through(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)
    bridge = (((0 << 6) + 9) << 16) + MIDI.REC_Plug_MixLevel
    fl.values[bridge] = 1.0

    status = run(fl, {"mode": "record", "pattern": 4, "start": BAR, "length": PPQ, "seconds": 0.5,
                      "target": {"kind": "bridge"}, "points": None, "marker": "Verse"})

    assert status["events"] > 0
    assert {event for _, event, _ in fl.recorded} == {bridge}
    assert {value for _, _, value in fl.recorded} == {MIDI.FromMIDI_Max}, "the value it already had"


def test_fl_not_arming_fails_the_job_with_the_reason_and_leaves_fl_stopped(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR, arms=False)
    fl.install(monkeypatch)

    status = run(fl, ramp(2, 1))

    assert status["phase"] == "failed" and status["error"] == "FL did not arm recording"
    assert not fl.playing and fl.recorded == []


def test_one_job_at_a_time(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)
    flmcp._start_job([ramp(2, 1)], {})

    with pytest.raises(RuntimeError, match="already running"):
        flmcp._start_job([ramp(4, 1)], {})


def test_a_recording_refuses_to_start_while_fl_already_records(monkeypatch):
    # Toggling record would stop the person's own recording.
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)
    fl.armed = True

    with pytest.raises(RuntimeError, match="already recording"):
        flmcp._start_job([ramp(2, 1)], {})


def test_controls_map_to_fl_record_event_ids(monkeypatch):
    FakeFL().install(monkeypatch)
    effect = ((2 << 6) + 3) << 16

    assert flmcp._target_event({"kind": "channel", "channel": 5, "param": "pan"}) == (5 << 16) + MIDI.REC_Chan_Pan
    assert flmcp._target_event({"kind": "channel", "channel": 5, "param": 12}) == (5 << 16) + 0x8000 + 12
    assert flmcp._target_event({"kind": "mixer", "track": 2, "param": "volume"}) == (2 << 22) + MIDI.REC_Mixer_Vol
    assert flmcp._target_event({"kind": "effect", "track": 2, "slot": 3, "param": 4}) == (
        effect + MIDI.REC_Plug_Plugin_First + 4
    )
    with pytest.raises(ValueError, match="cannot automate"):
        flmcp._target_event({"kind": "mixer", "track": 2, "param": 4})


def test_a_job_stops_fl_first_when_it_is_already_playing(monkeypatch):
    # start() is FL's play/pause: called while FL plays, it would pause, not record.
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)
    fl.playing, fl.pos = True, 5

    status = run(fl, ramp(5, 1))

    assert status["phase"] == "done" and status["first"] >= 4 * BAR
    assert fl.recorded and not fl.playing and not fl.armed


def test_a_playback_refuses_to_start_while_fl_records(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)
    fl.armed = True

    with pytest.raises(RuntimeError, match="already recording"):
        flmcp._start_job([{"mode": "play", "start": 0, "length": PPQ, "seconds": 0.5, "tracks": True}], {})


def test_an_arm_fl_shows_after_the_job_failed_is_undone_before_it_reports(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR, arms=False)
    fl.install(monkeypatch)
    flmcp._start_job([ramp(2, 1)], {})
    while flmcp._job_status([], {})["phase"] != "cleanup":
        fl.tick()
    fl.armed, fl.arms = True, True  # FL's arm lands late, after the job gave up on it

    for _ in range(400):
        fl.tick()
        if flmcp._job_status([], {})["phase"] == "failed":
            break
    for _ in range(3):
        fl.tick()  # the disarm FL shows a tick later

    assert flmcp._job_status([], {})["error"] == "FL did not arm recording"
    assert not fl.armed and not fl.playing


def test_a_cancelled_job_leaves_fl_stopped_and_not_recording(monkeypatch):
    fl = FakeFL(song_ticks=16 * BAR)
    fl.install(monkeypatch)
    job = flmcp._start_job([ramp(5, 4)], {})["job"]
    while flmcp._job_status([], {})["phase"] != "running":
        fl.tick()

    assert flmcp._cancel_job([], {}) == job
    for _ in range(400):
        fl.tick()
        if flmcp._job_status([], {})["phase"] == "failed":
            break

    assert flmcp._job_status([], {})["error"] == "the engine stopped waiting for it"
    assert not fl.playing and not fl.armed
    assert flmcp._cancel_job([], {}) is None, "nothing left to cancel"


def test_the_controller_does_not_answer_fls_per_change_callbacks():
    """FL calls OnRefresh and OnDirtyMixerTrack for every change it shows, every few
    milliseconds while automation plays a mixer volume; a SysEx event for each, which the
    engine never read, went out until FL stopped answering for good (fl-mcp-03, 2026-11-10)."""
    for callback in ("OnRefresh", "OnDirtyChannel", "OnDirtyMixerTrack"):
        assert not hasattr(flmcp, callback), callback


def test_a_recording_reports_its_widest_step_between_ticks_per_span(monkeypatch):
    """The engine reads playback within one recording step of the line (a recorded value
    holds until the next tick's), so the job reports how wide that step was in each of 64
    spans of the take: one slow tick widens only its own span."""
    fl = FakeFL(song_ticks=16 * BAR, step=12)
    fl.install(monkeypatch)

    status = run(fl, ramp(9, 2))

    assert len(status["gaps"]) == flmcp.SAMPLE_ROWS_MAX
    # a span the ticks skipped holds 0; the engine also uses each span's neighbours
    assert set(status["gaps"]) <= {0, 12} and status["gaps"].count(12) >= flmcp.SAMPLE_ROWS_MAX // 2
