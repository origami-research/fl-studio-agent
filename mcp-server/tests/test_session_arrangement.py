"""Patterns, their clips in the song, and automation, live through FL's recording.

The controller is scripted with the answers FL 2026 gave live (controller API 45,
docs/research/arrangement-automation-live-2026-11-09.md): findFirstNextEmptyPat selects the
next empty pattern, clonePattern copies into a chosen one, a recording job reports how many
values FL took, and FL adds a pattern's clip only before that pattern's first clip. Each test
names the rule it holds: results are FL's readback, nothing is recorded before an argument is
known good, and automation is accepted only when FL plays it back as asked.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from session_fakes import HELLO, FakeControl, make_session, snapshot_answer

from fl_mcp import errors, sdk
from fl_mcp.actions import ActionLog
from fl_mcp.schemas import AutomationPoint, PatternInfo, PlacedClip
from fl_mcp.session import WriteRejected
from fl_mcp.session import arrangement as arrangement_mod

PPQ = 96
CHANNELS = ["Kick", "Bass", "Lead", "Pad"]
LEAD_PARAMS = ["Volume", "Filter cutoff", "Filter resonance"]


class FakeSong:
    """FL's patterns, recording jobs and playback as the controller answers them."""

    def __init__(self) -> None:
        self.names = {1: "Pattern 1"}  # the template's pattern 1, already in the song
        self.lengths = {1: 16}  # 16th steps
        self.selected = 1
        self.jobs: list[dict[str, Any]] = []
        self.active: dict[int, list[int]] = {}  # start tick -> tracks FL reports playing there
        self.events = 3
        self.playback_offset = 0.0
        self.playback_lag = 0  # ticks: FL plays the value it recorded this much earlier
        self.gaps: list[int] = []  # the recording's widest step per span of the take, as the controller reports
        self.flat: float | None = None  # FL plays this value back, whatever was recorded
        self.fail: str | None = None

    def default(self, n: int) -> bool:
        """Empty and unchanged: never named or given notes."""
        return n not in self.names

    def status(self, args: list[Any]) -> dict[str, Any]:
        spec = self.jobs[-1]
        status = {"id": len(self.jobs), "phase": "failed" if self.fail else "done", "error": self.fail}
        if spec["mode"] == "record":
            status["events"] = self.events
            status["gaps"] = list(self.gaps)
            return status
        status["tracks"] = list(self.active.get(spec["start"] - 2, []))
        record = next((j for j in reversed(self.jobs) if j["mode"] == "record" and j["start"] == spec["start"]), None)
        rows = []
        if record and spec.get("target"):
            points = record["points"]
            for tick in range(spec["start"], spec["start"] + spec["length"], 24):
                at = max(0, tick - spec["start"] - self.playback_lag)
                value = _line(points, at) if self.flat is None else self.flat
                rows.append([tick, value + self.playback_offset])
        status["rows"] = rows
        return status

    def handlers(self) -> dict[str, Any]:
        def next_empty(args):
            self.selected = next(n for n in range(1, 1000) if self.default(n))

        def clone(args):
            source, dest = args
            self.lengths[dest] = self.lengths[source]
            self.names[dest] = f"{self.names[source]} #2"

        def rename(args):
            number, name = args
            self.names[number] = name

        def start(args):
            self.jobs.append(dict(args[0]))
            return {"job": len(self.jobs)}

        return {
            "state.snapshot": lambda args: snapshot_answer(CHANNELS),
            "mixer.getCurrentTempo": lambda args: 120000,
            "general.getRecPPQ": lambda args: PPQ,
            "patterns.patternMax": lambda args: 999,
            "patterns.patternCount": lambda args: len(self.lengths),
            "patterns.patternNumber": lambda args: self.selected,
            "patterns.findFirstNextEmptyPat": next_empty,
            "patterns.isPatternDefault": lambda args: self.default(args[0]),
            "patterns.getPatternName": lambda args: self.names.get(args[0], f"Pattern {args[0]}"),
            "patterns.getPatternLength": lambda args: self.lengths.get(args[0], 16),
            "patterns.setPatternName": rename,
            "patterns.clonePattern": clone,
            "plugins.getParamCount": lambda args: len(LEAD_PARAMS) if args[0] == 2 else 0,
            "plugins.getParamName": lambda args: LEAD_PARAMS[args[0]],
            "mixer.trackCount": lambda args: 8,
            "mixer.isTrackPluginValid": lambda args: tuple(args) == (3, 0),
            "plugins.getPluginName": lambda args: "Fruity Filter",
            "ctl.record": start,
            "ctl.job": self.status,
        }


def _line(points: list[list[float]], tick: float) -> float:
    for (t0, v0), (t1, v1) in itertools.pairwise(points):
        if tick <= t1:
            return v0 + (v1 - v0) * (tick - t0) / (t1 - t0)
    return points[-1][1]


@pytest.fixture
def song(monkeypatch):
    monkeypatch.setattr(arrangement_mod, "_require_record_question_off", lambda session: None)
    fake = FakeSong()
    control = FakeControl(hello=HELLO, handlers=fake.handlers())
    session, _ = make_session(control=control)
    return fake, session, control


def records(fake: FakeSong) -> list[dict[str, Any]]:
    return [j for j in fake.jobs if j["mode"] == "record"]


# -- patterns -------------------------------------------------------------------------


def test_a_new_pattern_is_fls_next_empty_one_named_as_asked(song):
    fake, session, _ = song

    made = session.new_pattern("Drop")

    assert made == PatternInfo(number=2, name="Drop", beats=4.0)
    assert fake.names[2] == "Drop"


def test_a_copy_holds_the_source_and_its_own_name(song):
    fake, session, control = song
    fake.names[2], fake.lengths[2] = "Verse", 32

    made = session.new_pattern("Verse 2", copy_of="Verse")

    assert made == PatternInfo(number=3, name="Verse 2", beats=8.0)
    assert ("patterns.clonePattern", [2, 3]) in control.calls


def test_an_overlong_name_is_refused_before_any_pattern_is_touched(song):
    fake, session, control = song

    with pytest.raises(errors.ToolError, match="1 to 64 characters"):
        session.new_pattern("x" * 65)
    assert not [op for op, _ in control.calls if op.startswith("patterns.find")]


def test_a_rename_fl_does_not_keep_raises_with_what_fl_shows(song):
    fake, session, control = song
    control.handlers["patterns.setPatternName"] = lambda args: None

    with pytest.raises(WriteRejected, match="named pattern 1 'Pattern 1', not 'Intro'"):
        session.rename_pattern(1, "Intro")


# -- placing clips ----------------------------------------------------------------------


def test_a_patterns_clips_go_in_from_the_last_bar_back(song):
    # FL adds a pattern's clip only before its first clip (live), so 17 must go in before 1.
    fake, session, _ = song
    fake.names[2], fake.lengths[2] = "Chorus", 32

    clips = session.place("Chorus", [1, 17])

    assert [j["start"] for j in records(fake)] == [16 * 4 * PPQ, 0]
    assert [c.bar for c in clips] == [1, 17]
    assert all(j["target"] == {"kind": "bridge"} and j.get("points") is None for j in records(fake))


def test_a_bar_after_the_patterns_first_clip_is_refused_before_anything_records(song):
    fake, session, _ = song
    fake.names[2], fake.lengths[2] = "Chorus", 32
    session.place("Chorus", [9])
    before = len(fake.jobs)

    with pytest.raises(errors.ToolError, match="clip only before its first one") as raised:
        session.place("Chorus", [13, 25])
    assert "copy_of=2" in raised.value.fix
    assert len(fake.jobs) == before


def test_a_clips_track_is_the_one_fl_newly_reports_playing(song):
    fake, session, control = song
    fake.names[2], fake.lengths[2] = "Chorus", 32
    start = 8 * 4 * PPQ
    fake.active[start] = [1]  # FL already plays track 1 at bar 9

    real_status = fake.status

    def status(args):
        spec = fake.jobs[-1]
        if spec["mode"] == "record":
            fake.active[start] = [1, 5]
        return real_status(args)

    control.handlers["ctl.job"] = status

    (clip,) = session.place("Chorus", [9])

    assert clip == PlacedClip(pattern=2, name="Chorus", bar=9, beats=8.0, track=5)
    assert session._placed == [clip], "the playlist read adds what FL added this session"


def test_a_placement_fl_recorded_nothing_for_raises(song):
    fake, session, _ = song
    fake.names[2], fake.lengths[2] = "Chorus", 32
    fake.events = 0

    with pytest.raises(WriteRejected, match="recorded nothing at bar 5"):
        session.place("Chorus", [5])


def test_a_job_fl_stops_raises_its_reason(song):
    fake, session, _ = song
    fake.names[2], fake.lengths[2] = "Chorus", 32
    fake.fail = "FL did not arm recording"

    with pytest.raises(WriteRejected, match="FL did not arm recording"):
        session.place("Chorus", [5])


def test_a_job_whose_status_fl_stops_answering_is_cancelled(song):
    """The engine gives up on a job it can no longer follow and asks the controller to stop
    it, so FL is not left recording after the call failed (#328 review)."""
    fake, session, control = song
    fake.names[2], fake.lengths[2] = "Chorus", 32

    def silent(args):
        raise errors.ToolError("control_plane_unavailable", "FL Studio is busy and did not answer in time.", "Retry.")

    control.handlers["ctl.job"] = silent
    control.handlers["ctl.cancel"] = lambda args: None

    with pytest.raises(WriteRejected, match="stopped answering during"):
        session.place("Chorus", [5])
    assert [op for op, _ in control.calls].count("ctl.cancel") == 1


def test_placing_refuses_while_fl_would_ask_what_to_record(monkeypatch):
    # The question is modal: a recording started under it waits for a person.
    monkeypatch.setattr(arrangement_mod.sys, "platform", "win32")
    import fl_mcp.fl.install as install

    monkeypatch.setattr(install, "fl_settings", lambda section: {})
    fake = FakeSong()
    fake.names[2], fake.lengths[2] = "Chorus", 32
    session, _ = make_session(control=FakeControl(hello=HELLO, handlers=fake.handlers()))

    with pytest.raises(errors.ToolError, match="asks what to record") as raised:
        session.place("Chorus", [5])
    assert "Don't show this in the future" in raised.value.fix
    assert fake.jobs == []


# -- automation -------------------------------------------------------------------------


SWEEP = [AutomationPoint(position=0, value=0.1), AutomationPoint(position=16, value=0.9)]


def test_automation_records_into_a_new_pattern_named_after_the_control(song):
    fake, session, _ = song

    take = session.automate(SWEEP, bar=13, param="cutoff", channel=2)

    (job,) = records(fake)
    assert job["target"] == {"kind": "channel", "channel": 2, "param": 1}
    assert job["start"] == 12 * 4 * PPQ and job["length"] == 16 * PPQ
    assert job["points"] == [[0, 0.1], [16 * PPQ, 0.9]]
    assert take.target == "Lead Filter cutoff" and take.pattern.name == "Lead Filter cutoff"
    assert take.max_error < 1e-9 and take.points[0].value == pytest.approx(0.1)


def test_automation_fl_plays_back_away_from_the_line_raises(song):
    fake, session, _ = song
    fake.playback_offset = 0.1

    with pytest.raises(WriteRejected, match=r"up to 0\.10 away"):
        session.automate(SWEEP, bar=13, param="volume", channel=1)


PUMP = [
    AutomationPoint(position=b + x, value=v) for b in range(4) for x, v in ((0, 0.25), (0.35, 0.8), (0.98, 0.8))
]


def test_a_dip_on_each_kick_passes_within_one_recording_step(song):
    """FL plays a recorded value until the next tick's, so where a pumping dip falls or
    recovers steeply its playback trails the line by up to one recording step: live, 0.55
    off at the exact position (fl-mcp-03, 2026-11-10). Within the step it matches."""
    fake, session, _ = song
    fake.playback_lag = 4  # ticks, about one OnIdle tick at 120 BPM
    fake.gaps = [4] * 64

    take = session.automate(PUMP, bar=1, param="volume", mixer_track=3)

    assert take.max_error < 0.01  # the points reach FL in whole ticks


def test_a_dip_played_back_later_than_the_recording_step_raises(song):
    fake, session, _ = song
    fake.gaps, fake.playback_lag = [4] * 64, 12

    with pytest.raises(WriteRejected, match="away from the values asked for"):
        session.automate(PUMP, bar=1, param="volume", mixer_track=3)


def test_spans_shorter_than_a_step_use_the_step_that_ended_after_them(song):
    """A short take has spans shorter than one idle tick, so most spans hold no step; each read
    still allows the step that covers it."""
    fake, session, _ = song
    fake.playback_lag = 4
    fake.gaps = [4 if i % 3 == 2 else 0 for i in range(64)]

    take = session.automate(PUMP, bar=1, param="volume", mixer_track=3)

    assert take.max_error < 0.01


def test_one_slow_tick_widens_the_check_only_where_it_happened(song):
    """A playback that never moved (nothing recorded) must fail even when one idle tick of the
    recording was slow: a step as wide as the dip's release, allowed everywhere, would let a
    flat line pass (review of #325)."""
    fake, session, _ = song
    fake.gaps = [4] * 64
    fake.gaps[0] = 2 * PPQ
    fake.flat = 0.8

    with pytest.raises(WriteRejected, match="away from the values asked for"):
        session.automate(PUMP, bar=1, param="volume", mixer_track=3)


def test_an_effect_parameter_is_found_on_its_mixer_slot(song):
    fake, session, control = song
    filter_params = ["Mix", "Cutoff freq", "Resonance"]
    control.handlers["plugins.getParamCount"] = lambda args: 3
    control.handlers["plugins.getParamName"] = lambda args: filter_params[args[0]]

    take = session.automate(SWEEP, bar=1, param="cutoff", mixer_track=3, slot=0)

    assert records(fake)[0]["target"] == {"kind": "effect", "track": 3, "slot": 0, "param": 1}
    assert take.target == "Fruity Filter Cutoff freq on mixer track 3"


def test_automation_needs_exactly_one_control_owner(song):
    _, session, _ = song

    with pytest.raises(errors.ToolError, match="exactly one of channel and mixer_track"):
        session.automate(SWEEP, bar=1, param="volume")
    with pytest.raises(errors.ToolError, match='"volume" or "pan"'):
        session.automate(SWEEP, bar=1, param="cutoff", mixer_track=3)


def test_automating_a_pattern_again_must_start_where_its_clip_does(song):
    fake, session, _ = song
    first = session.automate(SWEEP, bar=13, param="volume", channel=1)

    again = session.automate(SWEEP, bar=13, param="volume", channel=1, pattern=first.pattern.number)
    assert again.pattern.number == first.pattern.number
    with pytest.raises(errors.ToolError, match="starts at bar 13, not bar 17"):
        session.automate(SWEEP, bar=17, param="volume", channel=1, pattern=first.pattern.number)


def test_the_action_sentences_say_what_fl_now_has(song):
    fake, session, _ = song
    fake.names[2], fake.lengths[2] = "Chorus", 32
    log = ActionLog(track_name=session.track_name)

    log.called("place", {"pattern": "Chorus", "bars": [1, 9]}, session.place("Chorus", [1, 9]))
    log.called("automate", {}, session.automate(SWEEP, bar=13, param="volume", channel=1))

    assert log.sentences() == [
        "Placed Chorus in the song at bars 1, 9",
        "Automated Bass volume over bars 13-16 in pattern 3",
    ]


# -- the SDK's arguments ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("points", "why"),
    [
        ([{"position": 1, "value": 0.1}, {"position": 4, "value": 0.9}], "first point is at beat 0"),
        ([{"position": 0, "value": 0.1}, {"position": 0, "value": 0.9}], "positions must increase"),
        ([{"position": 0, "value": 0.1}, {"position": 4, "value": 0.9, "tension": 0.5}], "straight lines"),
        ([{"position": 0, "value": 0.1}], "at least 2"),
    ],
)
def test_automation_points_are_checked_before_fl_records_anything(song, points, why):
    fake, session, _ = song

    with pytest.raises(errors.ToolError, match=why):
        sdk.FL(session).automate(points, bar=5, channel="Bass")
    assert fake.jobs == []


def test_place_takes_one_bar_or_several(song):
    fake, session, _ = song
    fake.names[2], fake.lengths[2] = "Chorus", 32
    fl = sdk.FL(session)

    assert [c.bar for c in fl.place("Chorus", 25)] == [25]
    with pytest.raises(errors.ToolError, match="greater than or equal to 1"):
        fl.place("Chorus", [0])


def test_stems_and_meters_hear_the_clips_placed_this_session(song, monkeypatch, tmp_path):
    # A take's stems of an arranged build found "no channel has notes": the check read
    # only the song FL opened, not the clips placed since.
    from fl_mcp.schemas import Note, PlaylistClip, SessionState
    from fl_mcp.session import controls

    fake, session, control = song
    control.handlers.update({"transport.getLoopMode": lambda args: 1, "transport.getSongLength": lambda args: 32 * 4 * PPQ})
    session.project = tmp_path / "song.flp"
    session.project.write_bytes(b"")
    monkeypatch.setattr(type(session), "playlist", lambda self: [PlaylistClip(track=None, start=64.0, length=16.0, pattern=3)])
    monkeypatch.setattr(type(session), "all_notes", lambda self: {(3, 1): [Note(40, 0, 1)]})
    state = SessionState(ppq=PPQ, channels=[c for c in session.channel_names()])

    heard = controls.playing_notes(session, state, 64.0, 80.0, "fl.render")

    assert heard == {0: False, 1: True, 2: False, 3: False}
