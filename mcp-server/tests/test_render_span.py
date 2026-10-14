"""A render records FL's LIVE arrangement length, never the saved file's.

Dev arena 2026-10-07: every render on a trial box was 7.885 s (the template's
four bars at 130 BPM) whatever the model built, because the length came from
the saved .flp and a trial FL never saves. These tests pin the live span:
song mode reads the playlist's end, pattern mode the selected pattern, both at
the live tempo, with typed refusals instead of a silent or runaway capture.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from session_fakes import HELLO, FakeArrangement, FakeControl, FakeSpool, make_session

from fl_mcp import errors
from fl_mcp.contract import FLLicense
from fl_mcp.schemas import SpoolResponse
from fl_mcp.session import FLSession, controls
from fl_mcp.template import template_path


def _rig(monkeypatch, tmp_path, arrangement, *, fl_license=FLLicense.LICENSED, project=None):
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
            **arrangement.handlers(),
            "transport.setSongPos": lambda args: None,
            "transport.start": lambda args: None,
            "transport.stop": lambda args: None,
        },
    )
    session, _ = make_session(control=control, transport=transport, project=project, fl_license=fl_license)
    monkeypatch.setattr(FLSession, "caps", property(lambda self: SimpleNamespace(bridge_live=True)))
    monkeypatch.setattr(controls.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(controls.audio, "wav_format", lambda path: (100, 44100))
    monkeypatch.setattr(controls.audio, "analyze_wav", lambda path: SimpleNamespace(integrated_lufs=-12.0))
    return session, control, transport


def _captured_seconds(transport: FakeSpool) -> list[float]:
    return [args["seconds"] for op, args in transport.sent if op == "capture_start"]


def test_song_mode_records_the_live_playlist_at_the_live_tempo(monkeypatch, tmp_path):
    """16 bars on the playlist at 120 BPM is 32 s, plus the decay tail."""
    arrangement = FakeArrangement(loop_mode=1, tempo_bpm=120.0, song_last_tick=64 * 96 - 1)
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement)

    receipt = session.render_wav(tmp_path / "mix.wav")

    assert receipt.span.source == "song"
    assert receipt.span.pattern is None
    assert receipt.span.beats == 64.0
    assert receipt.span.seconds == pytest.approx(32.0)
    assert receipt.span.capture_seconds == pytest.approx(32.0 + controls.RENDER_TAIL_SECONDS)
    assert _captured_seconds(transport) == [receipt.span.capture_seconds]


def test_pattern_mode_records_the_selected_pattern(monkeypatch, tmp_path):
    """FL counts pattern length in 16th steps: 128 steps is 8 bars, 13.71 s at 140."""
    arrangement = FakeArrangement(
        loop_mode=0, tempo_bpm=140.0, pattern=3, pattern_steps=128, song_last_tick=1535
    )
    session, control, transport = _rig(monkeypatch, tmp_path, arrangement)

    receipt = session.render_wav(tmp_path / "mix.wav")

    assert (receipt.span.source, receipt.span.pattern, receipt.span.beats) == ("pattern", 3, 32.0)
    assert receipt.span.seconds == pytest.approx(32 * 60 / 140)
    assert _captured_seconds(transport) == [receipt.span.capture_seconds]
    # The playlist does not play in pattern mode, so its length is not even read.
    assert "transport.getSongLength" not in [op for op, _ in control.calls]


def test_a_tempo_change_moves_the_next_render(monkeypatch, tmp_path):
    """The tempo is read live per render; nothing caches it from an earlier read or file."""
    arrangement = FakeArrangement(loop_mode=0, tempo_bpm=100.0, pattern_steps=64)
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement)

    slow = session.render_wav(tmp_path / "slow.wav").span
    arrangement.tempo_bpm = 140.0
    fast = session.render_wav(tmp_path / "fast.wav").span

    assert (slow.tempo_bpm, fast.tempo_bpm) == (100.0, 140.0)
    assert slow.seconds == pytest.approx(16 * 60 / 100)
    assert fast.seconds == pytest.approx(16 * 60 / 140)
    assert _captured_seconds(transport) == [slow.capture_seconds, fast.capture_seconds]


def test_an_empty_playlist_in_song_mode_refuses_before_capturing(monkeypatch, tmp_path):
    """Live 2026-10-07: song mode over an empty playlist plays silence (LUFS -120)."""
    arrangement = FakeArrangement(loop_mode=1, song_last_tick=0)
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement)

    with pytest.raises(errors.ToolError, match="playlist is empty") as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "render_failed"
    assert transport.sent == []


def test_a_span_under_one_bar_plays_the_minimum_and_says_so(monkeypatch, tmp_path):
    arrangement = FakeArrangement(loop_mode=1, tempo_bpm=120.0, song_last_tick=95)  # one beat
    session, _, _ = _rig(monkeypatch, tmp_path, arrangement)

    span = session.render_wav(tmp_path / "mix.wav").span

    assert span.beats == 1.0
    assert span.seconds == pytest.approx(controls.RENDER_MIN_BEATS * 60 / 120)
    assert span.note is not None and "minimum" in span.note


def test_a_span_past_the_maximum_refuses_before_capturing(monkeypatch, tmp_path):
    """64 bars at 20 BPM is 12.8 minutes: refused, never truncated into a partial render."""
    arrangement = FakeArrangement(loop_mode=1, tempo_bpm=20.0, song_last_tick=256 * 96 - 1)
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement)

    with pytest.raises(errors.ToolError, match="at most 600 s") as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "render_failed"
    assert transport.sent == []


def test_the_maximum_counts_the_decay_tail(monkeypatch, tmp_path):
    """599.8 s of song plus the 0.5 s tail is past the Bridge's 600 s capture
    limit: a typed refusal, not a validation crash at the capture send."""
    ticks = round(599.8 * 120 / 60 * 96)
    arrangement = FakeArrangement(loop_mode=1, tempo_bpm=120.0, song_last_tick=ticks - 1)
    session, _, transport = _rig(monkeypatch, tmp_path, arrangement)

    with pytest.raises(errors.ToolError, match=r"including its 0\.5 s tail") as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "render_failed"
    assert transport.sent == []


def test_an_unknown_loop_mode_is_fl_api_error(monkeypatch, tmp_path):
    session, _, transport = _rig(monkeypatch, tmp_path, FakeArrangement(loop_mode=7))

    with pytest.raises(errors.ToolError, match="unknown loop mode 7") as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "fl_api_error"
    assert transport.sent == []


def test_without_the_control_plane_there_is_no_span_and_no_capture(monkeypatch, tmp_path):
    """No file fallback: the capture itself needs the control wire to start FL."""
    session, _, transport = _rig(monkeypatch, tmp_path, FakeArrangement())
    session.control = None

    with pytest.raises(errors.ToolError) as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    assert excinfo.value.category == "control_plane_unavailable"
    assert transport.sent == []


@pytest.mark.parametrize("fl_license", list(FLLicense))
def test_the_saved_template_never_sets_the_length_and_nothing_is_saved(monkeypatch, tmp_path, fl_license):
    """The regression: the project file is the template's 4-bar loop, FL plays 16
    bars live. The render follows FL and never asks it to save, trial or not."""
    project = tmp_path / "agent.flp"
    shutil.copyfile(template_path(), project)
    before = project.read_bytes()
    arrangement = FakeArrangement(loop_mode=1, tempo_bpm=130.0, song_last_tick=64 * 96 - 1)
    session, control, _ = _rig(monkeypatch, tmp_path, arrangement, fl_license=fl_license, project=project)

    span = session.render_wav(tmp_path / "mix.wav").span

    assert span.beats == 64.0
    assert span.seconds == pytest.approx(64 * 60 / 130)
    assert "ctl.save" not in [op for op, _ in control.calls]
    assert project.read_bytes() == before
