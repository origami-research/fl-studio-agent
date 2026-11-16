"""Live controls: tempo, channel levels, transport, plugin params.

One backend per call, readback results, and the unsafe-at-current-time
refusal is retryable - never a fallthrough (addenda A6/A11).
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from session_fakes import (
    HELLO,
    RENDER_SPAN,
    TEMPLATE_CHANNELS,
    FakeControl,
    FakeSpool,
    arrangement,
    make_session,
    snapshot_answer,
)

from fl_mcp import errors, flp
from fl_mcp.schemas import ControlResponse, PluginParamModel, PluginParamSpec, RenderReceipt
from fl_mcp.session import FLSession, WriteRejected, controls
from fl_mcp.session import facade as facade_mod


@pytest.fixture()
def no_reload(monkeypatch):
    """Never let a unit test SIGKILL or launch FL; record the calls instead."""
    calls: list[bool] = []
    monkeypatch.setattr(FLSession, "_reload_fl", lambda self: calls.append(True))
    return calls


UNSAFE = ControlResponse(id=1, ok=False, err="Operation unsafe at current time (mid-cycle)")


# --- set_tempo ---------------------------------------------------------------


def test_set_tempo_live_returns_the_readback_not_the_request():
    control = FakeControl(
        hello=HELLO,
        handlers={
            "mixer.setCurrentTempo": lambda args: True,
            "mixer.getCurrentTempo": lambda args: 128500,  # BPM x 1000, clamped by FL
        },
    )
    session, _ = make_session(control=control)

    assert session.set_tempo(130.0) == 128.5
    assert ("mixer.setCurrentTempo", [130.0, 1]) in control.calls


def test_render_wav_refuses_when_the_live_bridge_is_unavailable(tmp_path, caplog):
    control = FakeControl(hello=HELLO, handlers=arrangement())
    session, _ = make_session(control=control)

    with caplog.at_level("WARNING"), pytest.raises(errors.ToolError, match="nothing was recorded") as refused:
        session.render_wav(tmp_path / "mix.wav")
    assert refused.value.category == "render_failed"
    assert any("Bridge is not live" in r.message for r in caplog.records)
    # The common cause on a user's machine is FL showing another song (2026-10-26 box run).
    assert "song prepared for you" in refused.value.message
    assert "open the FLMCP Agent song" in refused.value.fix


def test_render_waits_for_an_in_flight_write_then_proceeds(monkeypatch, tmp_path):
    """A render serializes behind the write whose saved state it needs."""
    session, _ = make_session(project=tmp_path / "agent.flp")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    entered_render = threading.Event()
    rendered: list[Path] = []
    render_errors: list[Exception] = []

    def write() -> None:
        with session._write_guard("write_notes"):
            started.set()
            assert release.wait(timeout=1)
        finished.set()

    worker = threading.Thread(target=write)
    worker.start()
    assert started.wait(timeout=1)
    monkeypatch.setattr(
        controls,
        "render_wav",
        lambda _session, output, **options: entered_render.set() or RenderReceipt(path=output, span=RENDER_SPAN),
    )

    def render() -> None:
        try:
            rendered.append(session.render_wav(tmp_path / "mix.wav"))
        except Exception as exc:  # pragma: no cover - failure is asserted below
            render_errors.append(exc)

    renderer = threading.Thread(target=render)
    renderer.start()
    assert not entered_render.wait(timeout=0.05), "render ran before the active write finished"
    release.set()
    worker.join(timeout=1)
    renderer.join(timeout=1)
    assert finished.is_set()
    assert not renderer.is_alive()
    assert render_errors == []
    assert [receipt.path for receipt in rendered] == [tmp_path / "mix.wav"]


def test_render_refusal_after_bounded_wait_names_the_remaining_write(monkeypatch, tmp_path):
    """A stuck write remains retryable without leaving render blocked forever."""
    session, _ = make_session(project=tmp_path / "agent.flp")
    started = threading.Event()
    release = threading.Event()

    def write() -> None:
        with session._write_guard("write_notes"):
            started.set()
            assert release.wait(timeout=1)

    worker = threading.Thread(target=write)
    worker.start()
    assert started.wait(timeout=1)
    monkeypatch.setattr(facade_mod, "RENDER_IN_FLIGHT_WRITE_WAIT_SECONDS", 0.01)

    with pytest.raises(errors.ConcurrentWrite) as excinfo:
        session.render_wav(tmp_path / "mix.wav")

    release.set()
    worker.join(timeout=1)
    assert excinfo.value.retryable is True
    assert "write_notes" in excinfo.value.message
    assert "Retry in 0.01 seconds" in excinfo.value.fix


def test_live_render_verification_failure_is_a_tool_error_not_a_type_error(monkeypatch, tmp_path):
    """A failed capture must preserve its render_failed category and recovery hint."""
    captured = tmp_path / "capture.wav"
    captured.write_bytes(b"RIFF" + b"\0" * 44)
    transport = FakeSpool()
    # FakeSpool needs spool-shaped receipts; isolate the only behavior this
    # test is proving instead of coupling it to transport internals.
    from fl_mcp.schemas import SpoolResponse

    transport.responses = {
        "capture_start": SpoolResponse(
            id=1, ok=True, path=str(captured), frames_requested=100, sample_rate=44100
        ),
        "capture_stop": SpoolResponse(
            id=2,
            ok=True,
            path=str(captured),
            frames_written=100,
            frames_requested=100,
            sample_rate=44100,
            drop_count=1,
        ),
    }
    control = FakeControl(
        hello=HELLO,
        handlers={
            **arrangement(),
            "transport.setSongPos": lambda args: None,
            "transport.start": lambda args: None,
            "transport.stop": lambda args: None,
        },
    )
    session, _ = make_session(control=control, transport=transport)
    monkeypatch.setattr(FLSession, "caps", property(lambda self: SimpleNamespace(bridge_live=True)))
    monkeypatch.setattr(controls.time, "sleep", lambda seconds: None)

    with pytest.raises(errors.ToolError) as excinfo:
        session.render_wav(tmp_path / "mix.wav")
    assert excinfo.value.category == "render_failed"


def _verified_live_render(monkeypatch, tmp_path, stop):
    """Set up a verified capture so stop acknowledgement is the only variable."""
    from fl_mcp.schemas import SpoolResponse

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
            "transport.setSongPos": lambda args: None,
            "transport.start": lambda args: None,
            "transport.stop": stop,
        },
    )
    session, _ = make_session(control=control, transport=transport, project=tmp_path / "live.flp")
    session.project.write_bytes(b"FLhd")
    monkeypatch.setattr(controls.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(controls.audio, "wav_format", lambda path: (100, 44100))
    monkeypatch.setattr(controls.audio, "integrated_lufs", lambda path: -12.0)
    return session, control


def test_live_render_resends_an_unacknowledged_stop_once_fl_answers(monkeypatch, tmp_path):
    """Regression: a slow first stop acknowledgement cannot discard a verified WAV; the
    stop is sent again after FL answers a ping (playback.stop)."""
    stops = 0

    def stop(args):
        nonlocal stops
        stops += 1
        if stops == 1:
            raise errors.control_plane_unavailable("transport.stop", 8.0)

    session, control = _verified_live_render(monkeypatch, tmp_path, stop)

    assert controls._live_render(session, tmp_path / "mix.wav", 1.0) == (tmp_path / "mix.wav", False)
    ops = [op for op, _ in control.calls]
    first = ops.index("transport.stop")
    assert ops.count("transport.stop") == 2 and "ping" in ops[first:ops.index("transport.stop", first + 1)]
    assert [op for op, _ in session.transport.sent] == ["capture_stop", "capture_start", "capture_stop"]


def test_live_render_returns_verified_wav_when_stop_stays_unacknowledged(monkeypatch, tmp_path, caplog):
    """A verified capture wins when FL does not answer either best-effort stop."""
    session, control = _verified_live_render(
        monkeypatch,
        tmp_path,
        lambda args: (_ for _ in ()).throw(errors.control_plane_unavailable("transport.stop", 8.0)),
    )

    assert controls._live_render(session, tmp_path / "mix.wav", 1.0) == (tmp_path / "mix.wav", False)
    assert [op for op, _ in control.calls].count("transport.stop") == 2
    assert "FL may still be playing; the next transport command re-seats it" in caplog.text


def test_live_render_reports_unacknowledged_stop_when_capture_fails(monkeypatch, tmp_path, caplog):
    """Capture failure remains render_failed; the cleanup hazard reaches the operator log."""
    session, _ = _verified_live_render(
        monkeypatch,
        tmp_path,
        lambda args: (_ for _ in ()).throw(errors.control_plane_unavailable("transport.stop", 8.0)),
    )
    session.transport.responses["capture_stop"] = SimpleNamespace(
        ok=False, path=None, frames_written=None, err="Bridge wrote no audio frames"
    )

    with caplog.at_level("WARNING"), pytest.raises(errors.ToolError, match="recorded no audio") as excinfo:
        controls._live_render(session, tmp_path / "mix.wav", 1.0)
    assert excinfo.value.category == "render_failed"
    assert "Bridge" not in excinfo.value.message
    assert any("transport.stop was unacknowledged" in r.message for r in caplog.records)


def test_live_render_stops_any_leftover_capture_before_it_starts(monkeypatch, tmp_path):
    """Box take base-phonk-opus (2026-11-02): one render left the Bridge armed, and every later
    render failed "could not start recording" for seven minutes. Every render now stops the
    Bridge's capture first; with nothing armed the Bridge just says so."""
    from fl_mcp.schemas import SpoolResponse

    session, _ = _verified_live_render(monkeypatch, tmp_path, lambda args: None)
    good_stop = session.transport.responses["capture_stop"]
    session.transport.responses["capture_stop"] = [SpoolResponse(id=1, ok=False, err="no active capture"), good_stop]

    assert controls._live_render(session, tmp_path / "mix.wav", 1.0) == (tmp_path / "mix.wav", False)
    assert [op for op, _ in session.transport.sent] == ["capture_stop", "capture_start", "capture_stop"]


@pytest.mark.parametrize(("lufs", "silent"), [(-63.7, False), (-69.9, False), (-70.0, True), (-120.0, True)])
def test_one_silence_threshold_judges_every_render(monkeypatch, tmp_path, lufs, silent):
    """The audit of 0.1.14: a whole-mix render was refused as silent at -60 LUFS while
    Render.silent meant -70, so a quiet but audible mix (a solo hat measured -63.7 LUFS) was
    refused whole and accepted solo. audio.SILENCE_LUFS is the one definition for both."""
    session, _ = _verified_live_render(monkeypatch, tmp_path, lambda args: None)
    monkeypatch.setattr(controls.audio, "integrated_lufs", lambda path: lufs)

    assert controls._live_render(session, tmp_path / "solo.wav", 1.0, refuse_silence=False) == (
        tmp_path / "solo.wav", silent
    )
    if silent:
        with pytest.raises(errors.ToolError, match="recorded silence"):
            controls._live_render(session, tmp_path / "mix.wav", 1.0)
    else:
        assert controls._live_render(session, tmp_path / "mix.wav", 1.0) == (tmp_path / "mix.wav", False)


def test_a_clearing_stop_that_fails_does_not_block_the_render(monkeypatch, tmp_path, caplog):
    session, _ = _verified_live_render(monkeypatch, tmp_path, lambda args: None)
    good_stop = session.transport.responses["capture_stop"]
    session.transport.responses["capture_stop"] = [errors.note_plane_unavailable(1, 2.0), good_stop]

    with caplog.at_level("WARNING"):
        assert controls._live_render(session, tmp_path / "mix.wav", 1.0) == (tmp_path / "mix.wav", False)
    assert "could not clear the Bridge's capture" in caplog.text


def test_save_project_uses_the_control_lane():
    """Regression: liveness work must not leave ctl.save an expired timeout."""
    seen_timeouts: list[float] = []

    class TimeoutRecordingControl(FakeControl):
        def send_request(self, op, args=None, kwargs=None, timeout=2.0):
            seen_timeouts.append(timeout)
            return super().send_request(op, args, kwargs, timeout)

    control = TimeoutRecordingControl(hello=HELLO, handlers={"ctl.save": lambda args: 8})
    session, _ = make_session(control=control)

    assert session.save_project() is None
    assert ("ctl.save", []) in control.calls
    assert seen_timeouts[-1] == controls.LIVE_ANSWER_SECONDS


def test_save_project_retries_a_busy_control_wire():
    attempts = 0

    def save(args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise errors.control_plane_busy("ctl.save", 8.0)
        return 8

    control = FakeControl(hello=HELLO, handlers={"ctl.save": save})
    session, _ = make_session(control=control)

    assert session.save_project() is None
    assert attempts == 2


def test_save_project_requires_control_plane():
    session, _ = make_session()

    with pytest.raises(errors.ToolError) as excinfo:
        session.save_project()
    assert excinfo.value.category == "control_plane_unavailable"
    assert excinfo.value.message == "FL Studio did not answer in time."


def test_set_tempo_live_unconfirmed_raises():
    control = FakeControl(hello=HELLO, handlers={"mixer.setCurrentTempo": lambda args: True})
    session, _ = make_session(control=control)

    with pytest.raises(WriteRejected, match="did not report it back"):
        session.set_tempo(130.0)


@pytest.mark.parametrize("control", [None, FakeControl(hello=None)])
def test_set_tempo_never_rewrites_the_song_when_fl_does_not_answer(monkeypatch, tmp_path, no_reload, control):
    """Until 0.1.15 a tempo FL did not answer was written into the saved song and FL was
    killed and reopened on it, losing FL's unsaved work (a dialog open was enough). Now it
    is FL's answer or an error, like every other control."""
    monkeypatch.setattr(flp, "set_tempo", lambda *a: pytest.fail("must not rewrite the saved song"))
    session, _ = make_session(control=control, project=tmp_path / "agent.flp")

    with pytest.raises(errors.ToolError) as raised:
        session.set_tempo(137.0)
    assert raised.value.category == "control_plane_unavailable"
    assert no_reload == []


def test_set_tempo_unsafe_refusal_never_falls_through(monkeypatch, tmp_path, no_reload):
    """The warm-up window after _reload_fl (A6): degrade to the file backend
    here and you have rebuilt the v1 cascade bug."""
    monkeypatch.setattr(flp, "set_tempo", lambda *a: pytest.fail("must not fall through to the file backend"))
    control = FakeControl(hello=HELLO, handlers={"mixer.setCurrentTempo": lambda args: UNSAFE})
    session, _ = make_session(control=control, project=tmp_path / "agent.flp")

    with pytest.raises(errors.ToolError) as excinfo:
        session.set_tempo(130.0)
    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable is True
    assert no_reload == []


# --- set_channel_levels ------------------------------------------------------


def test_levels_live_all_fields_with_readback():
    control = FakeControl(
        hello=HELLO,
        handlers={
            "channels.setChannelVolume": lambda args: True,
            "channels.setChannelPan": lambda args: True,
            "channels.setChannelPitch": lambda args: True,
            "channels.getChannelVolume": lambda args: 0.5,
            "channels.getChannelPan": lambda args: -0.25,
            "channels.getChannelPitch": lambda args: 12.0,
        },
    )
    session, _ = make_session(control=control)

    levels = session.set_channel_levels(2, volume=0.6, pan=-0.3, pitch_semitones=12)

    # The result is FL's readback, not an echo of the request.
    assert (levels.volume, levels.pan, levels.pitch_semitones) == (0.5, -0.25, 12)
    assert ("channels.setChannelPitch", [2, 12, 1, 0, 1]) in control.calls


def test_levels_live_partial_failure_raises_and_never_falls_through(monkeypatch, tmp_path, no_reload):
    """The v1 bug this lane exists to fix: volume lands live, pan fails, and the
    fallback reloads FL from the file - silently discarding the volume. v2
    raises instead, naming what was kept."""
    monkeypatch.setattr(
        flp,
        "set_channel_levels",
        lambda *a, **k: pytest.fail("must not fall through to the file backend"),
    )
    control = FakeControl(hello=HELLO, handlers={"channels.setChannelVolume": lambda args: True})
    session, _ = make_session(control=control, project=tmp_path / "agent.flp")

    with pytest.raises(WriteRejected, match="KEPT: volume"):
        session.set_channel_levels(2, volume=0.6, pan=-0.3)
    assert no_reload == []


def test_levels_never_rewrite_the_song_when_fl_does_not_answer(monkeypatch, tmp_path, no_reload):
    """As for the tempo: FL's answer or an error, never the saved song rewritten and FL reopened."""
    monkeypatch.setattr(flp, "set_channel_levels", lambda *a, **k: pytest.fail("must not rewrite the saved song"))
    session, _ = make_session(project=tmp_path / "agent.flp")

    with pytest.raises(errors.ToolError) as raised:
        session.set_channel_levels(2, volume=0.6, pan=-0.3)
    assert raised.value.category == "control_plane_unavailable"
    assert no_reload == []


def test_levels_require_at_least_one_field():
    session, _ = make_session(control=FakeControl(hello=HELLO))
    with pytest.raises(ValueError, match="nothing to change"):
        session.set_channel_levels(2)


# --- undo (addendum A11) ------------------------------------------------------


def _rack(**handlers):
    """FL answering an undo: its channels' mutes read before and after it."""
    return FakeControl(hello=HELLO, handlers={"state.snapshot": lambda args: snapshot_answer(TEMPLATE_CHANNELS), **handlers})


def test_undo_sends_its_op_and_returns_none():
    control = _rack(**{"general.undo": lambda args: 0})
    session, _ = make_session(control=control)

    assert session.undo() is None
    assert [op for op, _ in control.calls if op != "ping"] == ["state.snapshot", "general.undo", "state.snapshot"]


def test_an_undo_that_reached_a_renders_mute_change_sets_it_back_and_says_so():
    """Box run 2026-11-03: FL keeps a solo render's mute changes in its undo history, so an
    undo right after one muted a channel again and the next render lost it."""
    muted = {"1": False}
    answer = snapshot_answer(TEMPLATE_CHANNELS)

    def snapshot(args):
        return {**answer, "channels": [{**c, "muted": c["idx"] == 1 and muted["1"]} for c in answer["channels"]]}

    def set_mutes(args):
        (pairs,) = args
        muted.update({str(index): bool(value) for index, value in pairs})
        return [[index, muted[str(index)]] for index, _ in pairs]

    control = _rack(**{
        "state.snapshot": snapshot,
        "general.undo": lambda args: muted.update({"1": True}),
        "ctl.set_mutes": set_mutes,
    })
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError, match="mute change from a solo or mute render") as excinfo:
        session.undo()

    assert muted["1"] is False
    assert ("ctl.set_mutes", [[[1, 0]]]) in control.calls
    assert "write the track again" in excinfo.value.fix


def test_an_undo_that_changed_the_channels_or_could_not_be_read_stands():
    """The guard compares like with like: a channel added or renamed by the undo, or a read
    that failed, leaves the undo as done rather than setting an unrelated channel's mute."""
    reads = iter([snapshot_answer(TEMPLATE_CHANNELS), snapshot_answer((*TEMPLATE_CHANNELS, "New"))])
    control = _rack(**{"state.snapshot": lambda args: next(reads), "general.undo": lambda args: 0})
    session, _ = make_session(control=control)

    assert session.undo() is None
    assert "ctl.set_mutes" not in [op for op, _ in control.calls]

    failing = _rack(**{"state.snapshot": lambda args: ControlResponse(id=1, ok=False, err="timed out"),
                       "general.undo": lambda args: 0})
    session, _ = make_session(control=failing)
    assert session.undo() is None
    assert "general.undo" in [op for op, _ in failing.calls]


def test_undo_raises_without_a_control_plane():
    session, _ = make_session()
    with pytest.raises(errors.ToolError) as excinfo:
        session.undo()
    assert excinfo.value.category == "control_plane_unavailable"


def test_a_persistent_unsafe_refusal_is_retryable_operation_unsafe():
    """Addendum A6: unsafe-at-current-time means busy, not broken. The link
    retried it to exhaustion; what surfaces is one plain, retryable sentence."""
    control = _rack(**{"general.undo": lambda args: UNSAFE})
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as excinfo:
        session.undo()
    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable is True
    assert "general.undo" not in excinfo.value.message


def test_transport_other_refusals_surface_fl_detail():
    boom = ControlResponse(id=1, ok=False, err="no such op")
    control = _rack(**{"general.undo": lambda args: boom})
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as excinfo:
        session.undo()
    assert excinfo.value.category == "fl_api_error"
    assert "no such op" in excinfo.value.message


# --- plugin parameters (live-only; E2-verified ops) --------------------------


def _param_control(values: dict[int, float] | None = None) -> FakeControl:
    """A generator with three params, mirroring the real wire ops."""
    state = values if values is not None else {0: 0.8, 1: 1.0, 2: 0.75}
    names = {0: "Main - Master level", 1: "Main - Master LFO level", 2: "Main - Master pitch"}
    quantum = 1 / 64  # FL quantizes to the control's grid (E2: 0.42 -> 0.41875)

    def set_value(args):
        value, index, _ch, _slot, _pickup, _global = args
        state[index] = round(value / quantum) * quantum
        return None

    return FakeControl(
        hello=HELLO,
        handlers={
            "plugins.getParamCount": lambda args: len(state),
            "plugins.getPluginName": lambda args: "Test Synth",
            "plugins.getParamName": lambda args: names[args[0]],
            "plugins.getParamValue": lambda args: state[args[0]],
            "plugins.setParamValue": set_value,
        },
    )


def test_plugin_params_lists_and_filters():
    session, _ = make_session(control=_param_control())

    all_params = session.plugin_params(0)
    assert [(p.index, p.name) for p in all_params] == [
        (0, "Main - Master level"),
        (1, "Main - Master LFO level"),
        (2, "Main - Master pitch"),
    ]
    lfo = session.plugin_params(0, query="lfo")
    assert [p.index for p in lfo] == [1]
    assert session.plugin_params(0, limit=2) == all_params[:2]


def test_set_plugin_param_returns_quantized_readback():
    session, _ = make_session(control=_param_control())

    confirmed = session.set_plugin_param(0, 0, 0.42)
    assert confirmed.name == "Main - Master level"
    assert confirmed.value == pytest.approx(0.421875)  # the grid, not the echo


def test_set_plugin_param_uses_the_index_advertised_by_params_on_the_generic_wire():
    control = _param_control()
    session, _ = make_session(control=control)

    param = session.plugin_params(0, query="pitch")[0]
    confirmed = session.set_plugin_param(0, param.index, 0.5)

    assert confirmed.index == param.index == 2
    assert ("plugins.setParamValue", [0.5, 2, 0, -1, 0, 1]) in control.calls


def _effect_control() -> FakeControl:
    """The same plugin as an effect in slot 0 of mixer track 17, the only effect in the mixer."""
    control = _param_control()
    control.handlers.update({
        "mixer.trackCount": lambda args: 127,
        "mixer.isTrackPluginValid": lambda args: int(tuple(args) == (17, 0)),
        # FL's own text for a value, which some plugins give (live: Fruity Limiter "-35.4dB")
        "plugins.getParamValueString": lambda args: "-35.4dB" if args[0] == 2 else "",
    })
    return control


def test_an_effects_parameters_read_and_set_through_its_mixer_slot():
    """A sidechain limiter is tuned through FL's documented plugin calls with the mixer
    track and slot in place of the channel and -1 (live: Fruity Limiter, 18 params)."""
    control = _effect_control()
    session, _ = make_session(control=control)

    assert [p.name for p in session.effect_params(17, 0, query="lfo")] == ["Main - Master LFO level"]
    confirmed = session.set_effect_param(17, 0, "pitch", 0.5)

    assert (confirmed.index, confirmed.value, confirmed.display) == (2, 0.5, "-35.4dB")
    assert [p.display for p in session.effect_params(17, 0)] == ["0.8", "1", "-35.4dB"], "FL's text, else the number"
    assert ("plugins.setParamValue", [0.5, 2, 17, 0, 0, 1]) in control.calls
    assert ("plugins.getParamValue", [2, 17, 0, 1]) in control.calls, "read back from the effect, not a channel"


@pytest.mark.parametrize(
    ("track", "slot", "words"),
    [(17, 3, "mixer track 17 has no effect in slot 3"), (200, 0, "does not exist"), (17, 10, "slots are 0 to 9")],
)
def test_a_slot_without_an_effect_is_refused_before_fl_is_asked_for_its_parameters(track, slot, words):
    control = _effect_control()
    session, _ = make_session(control=control)

    for call in (lambda: session.effect_params(track, slot), lambda: session.set_effect_param(track, slot, 0, 0.5)):
        with pytest.raises(errors.ToolError, match=words) as raised:
            call()
        assert raised.value.category == "invalid_argument"
    assert not [op for op, _ in control.calls if op.startswith("plugins.")]


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.plugin_params(3),
        lambda s: s.set_plugin_param(3, "cutoff", 0.5),
        lambda s: s.set_plugin_param(3, 0, 0.5),
    ],
)
def test_a_sampler_channel_has_no_parameters_and_says_so(call):
    """Live: FL answers every plugins.* call on a sampler with "Plugin not valid". Thirteen
    of 28 showcase runs (2026-11-15) asked a drum sampler for its parameters and got that
    as an FL failure; it is the channel's kind, so the error names it and what to use."""
    control = FakeControl(
        hello=HELLO,
        handlers={
            "channels.getChannelName": lambda args: "Hat",
            "plugins.getParamCount": lambda args: ControlResponse(id=1, ok=False, err="RuntimeError: Plugin not valid"),
        },
    )
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as raised:
        call(session)
    assert raised.value.category == "invalid_argument"
    assert raised.value.message == "Hat is a sampler channel, not a plugin, so it has no parameters."
    assert "set_levels" in raised.value.fix and "effect" in raised.value.fix
    assert not [op for op, _ in control.calls if op == "plugins.setParamValue"]


def test_an_effect_answering_plugin_not_valid_is_still_an_fl_failure():
    """Only a channel's own instrument slot means "a sampler"; a mixer effect slot that
    FL refuses is not reinterpreted."""
    control = _effect_control()
    control.handlers["plugins.getParamCount"] = lambda args: ControlResponse(
        id=1, ok=False, err="RuntimeError: Plugin not valid"
    )
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as raised:
        session.effect_params(17, 0)
    assert raised.value.category == "fl_api_error"


@pytest.mark.parametrize("index", [-1, 3, True, 1.5])
def test_set_plugin_param_rejects_indexes_not_advertised_by_params(index):
    control = _param_control()
    session, _ = make_session(control=control)

    with pytest.raises((TypeError, ValueError), match="index"):
        session.set_plugin_param(0, index, 0.5)

    assert not [call for call in control.calls if call[0] == "plugins.setParamValue"]


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_set_plugin_param_rejects_non_finite_or_non_numeric_values(value):
    session, _ = make_session(control=_param_control())

    with pytest.raises((TypeError, ValueError), match="finite normalized number"):
        session.set_plugin_param(0, 0, value)


def test_set_plugin_param_rejects_string_value_without_a_captured_model():
    # A str is a display/option value; without a captured model there is no
    # honest way to interpret it, so it is rejected before FL is asked to write.
    session, _ = make_session(control=_param_control())

    with pytest.raises(ValueError, match="captured per-plugin parameter model"):
        session.set_plugin_param(0, 0, "0.5")


def test_set_plugin_param_resolves_unique_name_and_rejects_ambiguity():
    session, _ = make_session(control=_param_control())

    confirmed = session.set_plugin_param(0, "master pitch", 0.5)
    assert confirmed.index == 2

    with pytest.raises(errors.ToolError, match="ambiguous"):
        session.set_plugin_param(0, "Master", 0.5)
    with pytest.raises(errors.ToolError, match="unknown"):
        session.set_plugin_param(0, "cutoff", 0.5)


def test_plugin_param_model_enriches_all_kinds_and_sets_display_values():
    model = PluginParamModel(
        plugin="Test Synth",
        parameters=(
            PluginParamSpec(
                index=0,
                name="Main - Master level",
                kind="continuous",
                minimum=-60,
                maximum=0,
                step=0.1,
                unit="dB",
            ),
            PluginParamSpec(
                index=1,
                name="Main - Master LFO level",
                kind="stepped",
                options=("Off", "Slow", "Fast"),
            ),
            PluginParamSpec(
                index=2,
                name="Main - Master pitch",
                kind="enum",
                options=("Sine", "Saw", "Square", "Noise"),
            ),
        ),
    )
    session, _ = make_session(control=_param_control(), plugin_param_models=(model,))

    params = session.plugin_params(0)
    assert [(param.kind, param.display) for param in params] == [
        ("continuous", "-12 dB"),
        ("stepped", "Fast"),
        ("enum", "Square"),
    ]
    confirmed = session.set_plugin_param(0, "Main - Master pitch", "Noise")
    assert (confirmed.value, confirmed.display) == (1.0, "Noise")
    with pytest.raises(ValueError, match="invalid option"):
        session.set_plugin_param(0, 2, "Triangle")


def test_set_plugin_param_resolves_a_numeric_display_string_against_its_range():
    state = {0: 0.5}

    def set_value(args):
        state[args[1]] = args[0]

    control = FakeControl(
        hello=HELLO,
        handlers={
            "plugins.getParamCount": lambda args: 1,
            "plugins.getPluginName": lambda args: "Test Synth",
            "plugins.getParamName": lambda args: "Cutoff",
            "plugins.getParamValue": lambda args: state[args[0]],
            "plugins.setParamValue": set_value,
        },
    )
    model = PluginParamModel(
        plugin="Test Synth",
        parameters=(PluginParamSpec(index=0, name="Cutoff", kind="continuous", minimum=20, maximum=1000),),
    )
    session, _ = make_session(control=control, plugin_param_models=(model,))

    session.set_plugin_param(0, "Cutoff", "440")

    assert ("plugins.setParamValue", [pytest.approx(420 / 980), 0, 0, -1, 0, 1]) in control.calls


def test_bool_param_accepts_option_and_round_trips():
    model = PluginParamModel(
        plugin="Test Synth",
        parameters=(
            PluginParamSpec(
                index=0,
                name="Main - Master level",
                kind="bool",
                options=("Off", "On"),
            ),
        ),
    )
    session, _ = make_session(control=_param_control({0: 0.0}), plugin_param_models=(model,))

    confirmed = session.set_plugin_param(0, 0, "On")
    assert (confirmed.kind, confirmed.value, confirmed.display) == ("bool", 1.0, "On")


def test_plugin_params_without_a_wire_is_an_honest_error():
    """Plugin state has no file representation - live-or-error, no fallback."""
    session, _ = make_session(control=None)
    with pytest.raises(errors.ToolError) as excinfo:
        session.plugin_params(0)
    assert excinfo.value.category == "control_plane_unavailable"


def test_a_file_rewrite_saves_fls_live_work_first(monkeypatch):
    from fl_mcp.session import lifecycle

    session, _ = make_session(control=FakeControl(hello=HELLO))
    events = []
    monkeypatch.setattr(controls, "save_project", lambda session: events.append("save"))
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: events.append("reload"))

    session._prepare_file_write()
    session._reload_fl()

    assert events == ["save", "reload"]


def test_each_reload_is_immediate(monkeypatch):
    from fl_mcp.session import lifecycle

    session, _ = make_session()
    reloads = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: reloads.append(1))
    session._reload_fl()
    session._reload_fl()
    assert reloads == [1, 1], "each reload is immediate when not batching"


def test_session_file_fallback_refuses_before_mutating_project(monkeypatch, tmp_path):
    """A failed live lane cannot turn into a hidden session file write/reboot."""
    project = tmp_path / "agent.flp"
    writes: list[bool] = []
    monkeypatch.setattr(flp, "set_tempo", lambda *args: writes.append(True))
    session, _ = make_session(project=project, session_spool_root=tmp_path)

    with pytest.raises(errors.ToolError) as excinfo:
        session.set_tempo(140)

    assert excinfo.value.category == "control_plane_unavailable"
    assert writes == []


def test_session_reload_refuses_without_touching_lifecycle(monkeypatch, tmp_path):
    """The session invariant applies even if a caller reaches _reload_fl directly."""
    from fl_mcp.session import lifecycle

    session, _ = make_session(project=tmp_path / "agent.flp", session_spool_root=tmp_path)
    reloads: list[bool] = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda *args, **kwargs: reloads.append(True))

    with pytest.raises(errors.ToolError) as excinfo:
        session._reload_fl()

    assert excinfo.value.category == "file_lane_requires_reload"
    assert reloads == []
