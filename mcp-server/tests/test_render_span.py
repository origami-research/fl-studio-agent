"""A render records FL's live arrangement length at the live tempo, never the saved file's."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from session_fakes import (
    FIVE_CHANNEL_PROJECT,
    HELLO,
    FakeControl,
    FakeSpool,
    arrangement,
    make_session,
    snapshot_answer,
)

from fl_mcp import errors
from fl_mcp.schemas import Note, SpoolResponse
from fl_mcp.session import FLSession, controls


def _rig(monkeypatch, tmp_path, handlers, *, project=None):
    """A verified Bridge capture whose only variable is FL's live arrangement."""
    captured = tmp_path / "capture.wav"
    captured.write_bytes(b"RIFF" + b"\0" * 45)
    transport = FakeSpool(
        responses={
            "capture_start": SpoolResponse(
                id=1, ok=True, path=str(captured), frames_requested=100, sample_rate=44100
            ),
            "capture_stop": SpoolResponse(
                id=2, ok=True, path=str(captured), frames_written=100, sample_rate=44100
            ),
        }
    )
    control = FakeControl(
        hello=HELLO,
        handlers={
            **handlers,
            "transport.setSongPos": lambda args: None,
            "transport.start": lambda args: None,
            "transport.stop": lambda args: None,
        },
    )
    session, _ = make_session(control=control, transport=transport, project=project)
    monkeypatch.setattr(FLSession, "caps", property(lambda self: SimpleNamespace(bridge_live=True)))
    monkeypatch.setattr(controls.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(controls.audio, "wav_format", lambda path: (100, 44100))
    monkeypatch.setattr(controls.audio, "integrated_lufs", lambda path: -12.0)
    return session, control, transport


def _captured_seconds(transport: FakeSpool) -> list[float]:
    return [args["seconds"] for op, args in transport.sent if op == "capture_start"]


@pytest.mark.parametrize(
    ("arrangements", "spans"),
    [
        # 16 bars on the playlist at 120 BPM.
        ([arrangement(loop_mode=1, tempo_bpm=120.0, song_last_tick=64 * 96 - 1)], [("song", None, 64.0, 32.0)]),
        # FL counts pattern length in 16th steps: 128 steps is 8 bars.
        ([arrangement(tempo_bpm=140.0, pattern=3, steps=128)], [("pattern", 3, 32.0, 32 * 60 / 140)]),
        # The tempo is read live per render, never cached.
        (
            [arrangement(tempo_bpm=100.0, steps=64), arrangement(tempo_bpm=140.0, steps=64)],
            [("pattern", 1, 16.0, 16 * 60 / 100), ("pattern", 1, 16.0, 16 * 60 / 140)],
        ),
    ],
    ids=["song", "pattern", "tempo-change"],
)
def test_a_render_records_the_live_span(monkeypatch, tmp_path, arrangements, spans):
    session, control, transport = _rig(monkeypatch, tmp_path, arrangements[0])
    got = []
    for handlers in arrangements:
        control.handlers.update(handlers)
        span = session.render_wav(tmp_path / "mix.wav").span
        got.append((span.source, span.pattern, span.beats, pytest.approx(span.seconds)))

    assert got == spans
    assert _captured_seconds(transport) == pytest.approx([s[3] + controls.RENDER_TAIL_SECONDS for s in spans])


def test_an_empty_playlist_in_song_mode_refuses_before_capturing(monkeypatch, tmp_path):
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement(loop_mode=1, song_last_tick=0))

    with pytest.raises(errors.ToolError, match="playlist is empty") as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "render_failed"
    assert transport.sent == []


@pytest.mark.parametrize(
    ("tempo_bpm", "seconds"),
    [(20.0, 768.0), (120.0, 599.8)],  # the second is under 600 s until the 0.5 s tail is added
    ids=["span", "span-plus-tail"],
)
def test_a_span_past_the_maximum_refuses_before_capturing(monkeypatch, tmp_path, tempo_bpm, seconds):
    ticks = round(seconds * tempo_bpm / 60 * 96)
    handlers = arrangement(loop_mode=1, tempo_bpm=tempo_bpm, song_last_tick=ticks - 1)
    session, _, transport = _rig(monkeypatch, tmp_path, handlers)

    with pytest.raises(errors.ToolError, match=r"at most 600 s including its 0\.5 s tail") as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "render_failed"
    assert transport.sent == []


def test_an_unknown_loop_mode_is_fl_api_error(monkeypatch, tmp_path):
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement(loop_mode=7))

    with pytest.raises(errors.ToolError, match="unknown loop mode 7") as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "fl_api_error"
    assert transport.sent == []


def test_without_the_control_plane_there_is_no_span_and_no_capture(monkeypatch, tmp_path):
    """No file fallback: the capture itself needs the control wire to start FL."""
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement())
    session.control = None

    with pytest.raises(errors.ToolError) as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "control_plane_unavailable"
    assert transport.sent == []


def test_the_saved_template_never_sets_the_length_and_nothing_is_saved(monkeypatch, tmp_path):
    """The project file is the template's 4-bar loop while FL plays 16 bars live."""
    project = tmp_path / "agent.flp"
    shutil.copyfile(FIVE_CHANNEL_PROJECT, project)
    before = project.read_bytes()
    handlers = arrangement(loop_mode=1, tempo_bpm=130.0, song_last_tick=64 * 96 - 1)
    session, control, _ = _rig(monkeypatch, tmp_path, handlers, project=project)

    span = session.render_wav(tmp_path / "mix.wav").span

    assert span.beats == 64.0
    assert span.seconds == pytest.approx(64 * 60 / 130)
    assert "ctl.save" not in [op for op, _ in control.calls]
    assert project.read_bytes() == before


# -- bars, solo, mute, stems (the model's own listening, docs/38) ------------------------


def _set_pos(control: FakeControl) -> list[list]:
    return [args for op, args in control.calls if op == "transport.setSongPos"]


def test_a_bar_range_plays_from_its_bar_and_records_only_its_length(monkeypatch, tmp_path):
    """16 bars at 120 BPM; bars 9-10 start at absolute tick 8 bars x 4 beats x 96 ppq."""
    session, control, transport = _rig(monkeypatch, tmp_path, arrangement(loop_mode=1, tempo_bpm=120.0, song_last_tick=64 * 96 - 1))

    receipt = session.render_wav(tmp_path / "mix.wav", start_bar=9, bars=2)

    assert (receipt.span.start_bar, receipt.span.beats) == (9, 8.0)
    assert receipt.span.seconds == pytest.approx(4.0)
    assert _captured_seconds(transport) == pytest.approx([4.0 + controls.RENDER_TAIL_SECONDS])
    assert _set_pos(control) == [[8 * 4 * 96, controls.FL_SONGLENGTH_ABSTICKS]]


def test_the_whole_song_still_starts_where_every_render_has(monkeypatch, tmp_path):
    session, control, _ = _rig(monkeypatch, tmp_path, arrangement(loop_mode=1, song_last_tick=64 * 96 - 1))

    session.render_wav(tmp_path / "mix.wav")

    assert _set_pos(control) == [[0]]


def test_a_range_past_the_end_is_cut_there_and_a_start_past_it_is_refused(monkeypatch, tmp_path):
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement(tempo_bpm=120.0, steps=64))  # a 4-bar pattern

    assert session.render_wav(tmp_path / "mix.wav", start_bar=3, bars=8).span.beats == 8.0
    with pytest.raises(errors.ToolError, match="pattern 1 is 4 bars long; start at bar 1 to 4") as excinfo:
        session.render_wav(tmp_path / "mix.wav", start_bar=5)
    assert excinfo.value.category == "invalid_argument"
    assert len(_captured_seconds(transport)) == 1


@pytest.mark.parametrize(("start_bar", "bars"), [(0, None), (1, 0), (True, None), (1, 2.5)])
def test_bars_and_start_bar_are_whole_bars(monkeypatch, tmp_path, start_bar, bars):
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement())

    with pytest.raises(errors.ToolError) as excinfo:
        session.render_wav(tmp_path / "mix.wav", start_bar=start_bar, bars=bars)

    assert excinfo.value.category == "invalid_argument"
    assert transport.sent == []


def test_a_long_song_can_still_render_a_few_of_its_bars(monkeypatch, tmp_path):
    """The ten-minute cap is on what is recorded, so bars of a song past it still render."""
    session, _, _ = _rig(monkeypatch, tmp_path, arrangement(loop_mode=1, tempo_bpm=20.0, song_last_tick=1024 * 96 - 1))

    assert session.render_wav(tmp_path / "mix.wav", start_bar=100, bars=1).span.seconds == pytest.approx(12.0)


def test_silence_is_refused_only_for_the_whole_mix(monkeypatch, tmp_path):
    """A silent whole mix means FL's audio path is broken; a silent range or solo is an answer."""
    session, _, _ = _rig(monkeypatch, tmp_path, arrangement(loop_mode=1, song_last_tick=64 * 96 - 1))
    monkeypatch.setattr(controls.audio, "integrated_lufs", lambda path: -120.0)

    assert session.render_wav(tmp_path / "intro.wav", bars=1).span.beats == 4.0
    with pytest.raises(errors.ToolError, match="recorded silence"):
        session.render_wav(tmp_path / "mix.wav")


BUSY = errors.ToolError("control_plane_unavailable", "FL Studio is busy and did not answer in time.", "Retry.")


class _Rack:
    """FL's channel rack mutes as the controller's ``ctl.set_mutes`` answers them: each
    [index, value] pair sets one channel (measured on FL 2026: 1 mutes, 0 unmutes, a repeat
    changes nothing) and the answer reads every one back."""

    def __init__(
        self, muted: dict[int, bool], *, miss: frozenset[tuple[str, int]] = frozenset(), freeze_after: int = 0
    ) -> None:
        self.muted = dict(muted)
        # Calls FL does not answer: ("set", n) the nth set_mutes, ("ping", n) the nth ping
        # since the first set_mutes; and after ``freeze_after`` set_mutes, nothing until thawed.
        self.miss, self.freeze_after, self.frozen = miss, freeze_after, False
        self.sets = self.pings = 0
        self.ops: list[dict[int, bool]] = []  # every set_mutes FL applied, in order

    @property
    def toggles(self) -> list[int]:
        return [index for op in self.ops for index in op]

    def handlers(self) -> dict:
        def set_mutes(args):
            self.sets += 1
            if self.frozen or ("set", self.sets) in self.miss:
                raise BUSY
            (pairs,) = args
            self.ops.append({index: bool(value) for index, value in pairs})
            self.muted.update(self.ops[-1])
            self.frozen = self.sets == self.freeze_after
            return [[index, self.muted[index]] for index, _ in pairs]

        def ping(args):
            self.pings += bool(self.ops)
            if self.frozen or ("ping", self.pings) in self.miss:
                raise BUSY
            return "pong"

        answer = snapshot_answer([f"ch{i}" for i in sorted(self.muted)])
        return {
            "state.snapshot": lambda args: {
                **answer,
                "channels": [{**c, "muted": self.muted[c["idx"]]} for c in answer["channels"]],
            },
            "ctl.set_mutes": set_mutes,
            "ping": ping,
        }


def test_solo_mutes_the_others_for_the_render_and_puts_every_mute_back(monkeypatch, tmp_path):
    """Channel 3 was muted by the user; solo 1 mutes 0 and 2, mute stays on 3, all as before after."""
    rack = _Rack({0: False, 1: False, 2: False, 3: True})
    session, control, _ = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})
    during = []
    real = controls._live_render
    monkeypatch.setattr(controls, "_live_render", lambda *a, **k: (during.append(dict(rack.muted)), real(*a, **k))[1])

    receipt = session.render_wav(tmp_path / "bass.wav", solo=(1,))

    assert during == [{0: True, 1: False, 2: True, 3: True}]
    assert rack.muted == {0: False, 1: False, 2: False, 3: True}
    assert (receipt.solo, receipt.mute) == ([1], [])
    # One controller op each way, and FL answered between the mutes and the play (#309:
    # a mute and a read per channel, 44 requests on 23 channels, and FL stopped answering).
    assert rack.ops == [{0: True, 2: True}, {0: False, 2: False}]
    ops = [op for op, _ in control.calls]
    assert ops.index("ctl.set_mutes") < len(ops) - 1 - ops[::-1].index("ping") < ops.index("transport.start")


def test_mute_silences_only_those_channels(monkeypatch, tmp_path):
    rack = _Rack({0: False, 1: False, 2: False})
    session, _, _ = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})

    session.render_wav(tmp_path / "no-drums.wav", mute=(0, 2))

    assert rack.ops == [{0: True, 2: True}, {0: False, 2: False}]  # muted for the render, then set back
    assert rack.muted == {0: False, 1: False, 2: False}


def test_the_mutes_go_back_when_the_render_fails(monkeypatch, tmp_path):
    rack = _Rack({0: False, 1: False})
    session, _, transport = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})
    transport.responses["capture_stop"] = SpoolResponse(id=2, ok=False, err="no audio")

    with pytest.raises(errors.ToolError, match="recorded no audio"):
        session.render_wav(tmp_path / "bass.wav", solo=(1,))

    assert rack.muted == {0: False, 1: False}


def test_nothing_plays_when_fl_does_not_answer_after_the_mutes(monkeypatch, tmp_path):
    rack = _Rack({0: False, 1: False}, miss=frozenset({("ping", 1)}))  # the ping after the mutes
    session, control, transport = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})

    with pytest.raises(errors.ToolError, match="nothing was played") as excinfo:
        session.render_wav(tmp_path / "bass.wav", solo=(1,))

    assert excinfo.value.category == "render_failed"
    assert "transport.start" not in [op for op, _ in control.calls] and transport.sent == []
    assert rack.muted == {0: False, 1: False}


def test_a_restore_fl_misses_is_sent_again_once_fl_answers(monkeypatch, tmp_path):
    rack = _Rack({0: False, 1: False}, miss=frozenset({("set", 2)}))  # the first restore
    session, _, _ = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})

    session.render_wav(tmp_path / "bass.wav", solo=(1,))

    assert rack.muted == {0: False, 1: False}
    assert session.unrestored_mutes == {}


def test_fl_going_silent_names_every_channel_it_may_have_left_and_the_next_render_sets_them_back(
    monkeypatch, tmp_path
):
    """#309: FL stopped answering during a solo render; the result must say which channels
    may still be muted, and the session sets them back when FL answers again."""
    rack = _Rack({0: False, 1: False, 2: True}, freeze_after=1)  # silent after the mutes
    session, _, transport = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})

    with pytest.raises(errors.ToolError) as excinfo:
        session.render_wav(tmp_path / "pad.wav", solo=(2,))

    message = excinfo.value.message
    assert "These may still be muted: ch0 (0), ch1 (1). These may still be unmuted: ch2 (2)." in message
    assert "already failed: FL did not answer after the channel mutes changed" in message
    assert "next solo or mute render sets them back" in excinfo.value.fix
    assert rack.muted == {0: True, 1: True, 2: False} and transport.sent == []

    rack.frozen = False  # FL answers again
    session.render_wav(tmp_path / "bass.wav", solo=(1,))

    assert rack.ops[1] == {0: False, 1: False, 2: True}  # the owed restore, before anything else
    assert rack.muted == {0: False, 1: False, 2: True}
    assert session.unrestored_mutes == {}


def test_an_owed_restore_skips_a_channel_that_is_no_longer_the_same(monkeypatch, tmp_path):
    rack = _Rack({0: False, 1: False})
    session, _, _ = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})
    session.unrestored_mutes.update({(0, "Bass"): True, (1, "ch1"): False})  # 0 is another channel now

    session.render_wav(tmp_path / "x.wav", mute=(1,))

    assert rack.ops == [{1: True}, {1: False}]
    assert session.unrestored_mutes == {}


@pytest.mark.parametrize(("solo", "mute", "message"), [((1,), (1,), "in both solo and mute"), ((9,), (), "no channel at index 9")])
def test_solo_and_mute_name_real_channels_once(monkeypatch, tmp_path, solo, mute, message):
    rack = _Rack({0: False, 1: False})
    session, _, transport = _rig(monkeypatch, tmp_path, {**arrangement(), **rack.handlers()})

    with pytest.raises(errors.ToolError, match=message):
        session.render_wav(tmp_path / "x.wav", solo=solo, mute=mute)

    assert rack.toggles == [] and transport.sent == []


def test_stems_record_each_channel_alone_one_play_each(monkeypatch, tmp_path):
    rack = _Rack({0: False, 1: False, 2: True})
    session, _, transport = _rig(monkeypatch, tmp_path, {**arrangement(tempo_bpm=120.0, steps=64), **rack.handlers()})

    stems = session.render_stems(tmp_path / "take.wav", "channels", bars=1).stems

    assert [s.path.name for s in stems] == ["take-0-ch0.wav", "take-1-ch1.wav", "take-2-ch2.wav"]
    assert [s.solo for s in stems] == [[0], [1], [2]]
    assert len(_captured_seconds(transport)) == 3
    assert rack.muted == {0: False, 1: False, 2: True}  # the user's mute on channel 2 is back
    # One rack for the set: each stem changes only what differs from the last (box run
    # 2026-11-03: re-muting every channel per stem took 12 s a stem on 23 channels).
    assert rack.toggles == [1, 0, 1, 1, 2, 0, 1, 2]


def test_mixer_stems_solo_the_channels_routed_to_each_track(monkeypatch, tmp_path):
    rack = _Rack({0: False, 1: False, 2: False})
    handlers = {**arrangement(tempo_bpm=120.0, steps=64), **rack.handlers()}
    snapshot = handlers["state.snapshot"]
    handlers["state.snapshot"] = lambda args: {
        **snapshot(args),
        "channels": [{**c, "target_mixer_track": 1 if c["idx"] < 2 else 2} for c in snapshot(args)["channels"]],
        "mixer": [{"idx": t, "name": f"Bus {t}", "vol": 0.8, "pan": 0.0, "muted": False, "solo": False} for t in range(3)],
    }
    session, _, transport = _rig(monkeypatch, tmp_path, handlers)

    stems = session.render_stems(tmp_path / "take.wav", "mixer", bars=1).stems

    assert [(s.path.name, s.solo) for s in stems] == [("take-mixer1-Bus-1.wav", [0, 1]), ("take-mixer2-Bus-2.wav", [2])]


def test_stems_past_the_recording_cap_are_refused_before_any_play(monkeypatch, tmp_path):
    rack = _Rack({i: False for i in range(12)})
    handlers = arrangement(loop_mode=1, tempo_bpm=60.0, song_last_tick=60 * 96 - 1)  # 60 s each
    session, _, transport = _rig(monkeypatch, tmp_path, {**handlers, **rack.handlers()})

    with pytest.raises(errors.ToolError, match=r"12 stems of 60\.0 s each") as excinfo:
        session.render_stems(tmp_path / "take.wav", "channels")

    assert excinfo.value.category == "render_failed"
    assert transport.sent == []


# -- stems skip the channels with no notes in the bars (box run 2026-11-03) ----------------


def _noted(monkeypatch, tmp_path, rack: _Rack, notes: dict, *, mixer=None):
    """A rig whose song (a project file FL opened) has ``notes`` by (pattern, channel)."""
    handlers = {**arrangement(tempo_bpm=120.0, steps=64), **rack.handlers()}
    if mixer is not None:
        snapshot = handlers["state.snapshot"]
        handlers["state.snapshot"] = lambda args: {
            **snapshot(args),
            "channels": [{**c, "target_mixer_track": mixer[c["idx"]]} for c in snapshot(args)["channels"]],
            "mixer": [{"idx": t, "name": f"Bus {t}", "vol": 0.8, "pan": 0.0, "muted": False, "solo": False}
                      for t in sorted(set(mixer.values()))],
        }
    project = tmp_path / "song.flp"
    project.write_bytes(b"FLhd")
    session, control, transport = _rig(monkeypatch, tmp_path, handlers, project=project)
    monkeypatch.setattr(FLSession, "all_notes", lambda self: notes)
    return session, transport


def test_stems_record_only_the_channels_with_notes_in_the_bars(monkeypatch, tmp_path):
    """Live: 20 of 23 stems were silent and took most of 6.5 minutes; each is a full play."""
    rack = _Rack({i: False for i in range(4)})
    kick, late = Note(key=36, start=0, length=1), Note(key=48, start=6, length=1)  # bar 1; bar 2 only
    session, transport = _noted(monkeypatch, tmp_path, rack, {(1, 0): [kick], (1, 2): [late], (2, 3): [kick]})

    receipt = session.render_stems(tmp_path / "take.wav", "channels", bars=1)

    assert [s.solo for s in receipt.stems] == [[0]]
    assert receipt.skipped == [1, 2, 3]  # no notes; notes in bar 2; notes in another pattern
    assert len(_captured_seconds(transport)) == 1
    assert [s.solo for s in session.render_stems(tmp_path / "take.wav", "channels").stems] == [[0], [2]]


def test_mixer_stems_record_only_the_tracks_fed_by_channels_with_notes(monkeypatch, tmp_path):
    rack = _Rack({i: False for i in range(4)})
    note = Note(key=36, start=0, length=1)
    session, _ = _noted(monkeypatch, tmp_path, rack, {(1, 1): [note]}, mixer={0: 1, 1: 2, 2: 2, 3: 3})

    receipt = session.render_stems(tmp_path / "take.wav", "mixer", bars=1)

    assert [(s.path.name, s.solo) for s in receipt.stems] == [("take-mixer2-Bus-2.wav", [1])]
    assert receipt.skipped == [0, 2, 3]


def test_named_stems_record_those_channels_notes_or_not(monkeypatch, tmp_path):
    rack = _Rack({i: False for i in range(3)})
    session, _ = _noted(monkeypatch, tmp_path, rack, {(1, 0): [Note(key=36, start=0, length=1)]})

    receipt = session.render_stems(tmp_path / "take.wav", "channels", bars=1, channels=(2, 1, 2))

    assert ([s.solo for s in receipt.stems], receipt.skipped) == ([[2], [1]], [])
    with pytest.raises(errors.ToolError, match="no channel at index 7"):
        session.render_stems(tmp_path / "take.wav", "channels", channels=(7,))
    with pytest.raises(errors.ToolError, match="cannot be mixer stems"):
        session.render_stems(tmp_path / "take.wav", "mixer", channels=(1,))


def test_stems_of_bars_with_no_notes_refuse_before_any_play(monkeypatch, tmp_path):
    rack = _Rack({i: False for i in range(2)})
    session, transport = _noted(monkeypatch, tmp_path, rack, {})

    with pytest.raises(errors.ToolError, match="No channel has notes in bar 1,") as excinfo:
        session.render_stems(tmp_path / "take.wav", "channels", bars=1)

    assert excinfo.value.category == "invalid_argument"
    assert transport.sent == [] and rack.toggles == []
