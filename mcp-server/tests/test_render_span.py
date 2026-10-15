"""A render records FL's live arrangement length at the live tempo, never the saved file's."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from session_fakes import HELLO, FakeControl, FakeSpool, arrangement, make_session

from fl_mcp import errors
from fl_mcp.schemas import SpoolResponse
from fl_mcp.session import FLSession, controls
from fl_mcp.template import template_path


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
    monkeypatch.setattr(controls.audio, "analyze_wav", lambda path: SimpleNamespace(integrated_lufs=-12.0))
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
    shutil.copyfile(template_path(), project)
    before = project.read_bytes()
    handlers = arrangement(loop_mode=1, tempo_bpm=130.0, song_last_tick=64 * 96 - 1)
    session, control, _ = _rig(monkeypatch, tmp_path, handlers, project=project)

    span = session.render_wav(tmp_path / "mix.wav").span

    assert span.beats == 64.0
    assert span.seconds == pytest.approx(64 * 60 / 130)
    assert "ctl.save" not in [op for op, _ in control.calls]
    assert project.read_bytes() == before
