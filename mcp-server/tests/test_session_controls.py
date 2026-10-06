"""Live controls: tempo, channel levels, transport, plugin params.

One backend per call, readback results, and the unsafe-at-current-time
refusal is retryable - never a fallthrough (addenda A6/A11).
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from session_fakes import HELLO, FakeControl, FakePlatform, FakeSpool, make_session

from fl_mcp import errors, flp
from fl_mcp.schemas import AutomationPoint, ControlResponse, PluginParamModel, PluginParamSpec
from fl_mcp.session import FLSession, NotePlaneUnavailable, WriteRejected, controls
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


def test_export_offline_saves_then_subprocesses_the_resolved_fl_path(monkeypatch, tmp_path):
    project = tmp_path / "live.flp"
    output = tmp_path / "mix.wav"
    project.write_bytes(b"FLhd")
    commands: list[list[str]] = []
    events: list[str] = []

    def render(command, check, timeout, env=None):
        events.append("render")
        commands.append(command)
        project.with_suffix(".wav").write_bytes(b"RIFF....WAVE")

    monkeypatch.setattr("fl_mcp.session.controls.subprocess.run", render)
    control = FakeControl(hello=HELLO, handlers={"ctl.save": lambda args: events.append("save")})
    session, _ = make_session(control=control, project=project)

    assert session.export_offline(output) == output.resolve()
    assert events == ["save", "render"]
    assert control.calls[-1] == ("ctl.save", [])
    assert commands == [[str(FakePlatform.render_executable), "-R", str(project.resolve())]]
    assert output.read_bytes() == b"RIFF....WAVE"


def test_render_wav_refuses_when_the_live_bridge_is_unavailable(tmp_path):
    project = tmp_path / "live.flp"
    output = tmp_path / "mix.wav"
    project.write_bytes(b"FLhd")

    control = FakeControl(hello=HELLO, handlers={"ctl.save": lambda args: None})
    session, _ = make_session(control=control, project=project)

    with pytest.raises(errors.ToolError, match="Bridge is not live"):
        session.render_wav(output)


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
    monkeypatch.setattr(controls, "render_wav", lambda _session, output: entered_render.set() or output)

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
    assert rendered == [tmp_path / "mix.wav"]


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


def test_render_guard_can_reenter_without_waiting_for_itself(tmp_path):
    """Render's save path runs under the render guard, so same-request nesting must not deadlock."""
    session, _ = make_session(project=tmp_path / "agent.flp")

    with session._write_guard("render_wav"), session._write_guard("save_project"):
        pass


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
            "ctl.save": lambda args: None,
            "transport.setSongPos": lambda args: None,
            "transport.start": lambda args: None,
            "transport.stop": lambda args: None,
        },
    )
    session, _ = make_session(control=control, transport=transport, project=tmp_path / "live.flp")
    session.project.write_bytes(b"FLhd")
    monkeypatch.setattr(FLSession, "caps", property(lambda self: SimpleNamespace(bridge_live=True)))
    monkeypatch.setattr(controls, "_render_span_seconds", lambda project: 0.0)
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
    monkeypatch.setattr(controls.audio, "_read_wav", lambda path: ([0.1] * 100, 44100))
    monkeypatch.setattr(controls.audio, "analyze_wav", lambda path: SimpleNamespace(integrated_lufs=-12.0))
    return session, control


def test_live_render_retries_an_unacknowledged_stop_after_capture(monkeypatch, tmp_path):
    """Regression: a slow first stop acknowledgement cannot discard a verified WAV."""
    stops = 0

    def stop(args):
        nonlocal stops
        stops += 1
        if stops == 1:
            raise errors.control_plane_unavailable("transport.stop", 8.0)

    session, control = _verified_live_render(monkeypatch, tmp_path, stop)

    assert controls._live_render(session, tmp_path / "mix.wav", 0.0) == tmp_path / "mix.wav"
    assert [op for op, _ in control.calls].count("transport.stop") == 2
    assert [op for op, _ in session.transport.sent] == ["capture_start", "capture_stop"]


def test_live_render_returns_verified_wav_when_stop_stays_unacknowledged(monkeypatch, tmp_path, caplog):
    """A verified capture wins when FL does not answer either best-effort stop."""
    session, control = _verified_live_render(
        monkeypatch,
        tmp_path,
        lambda args: (_ for _ in ()).throw(errors.control_plane_unavailable("transport.stop", 8.0)),
    )

    assert controls._live_render(session, tmp_path / "mix.wav", 0.0) == tmp_path / "mix.wav"
    assert [op for op, _ in control.calls].count("transport.stop") == 2
    assert "FL may still be playing; the next transport command re-seats it" in caplog.text


def test_live_render_reports_unacknowledged_stop_when_capture_fails(monkeypatch, tmp_path):
    """Capture failure remains render_failed and retains the cleanup hazard."""
    session, _ = _verified_live_render(
        monkeypatch,
        tmp_path,
        lambda args: (_ for _ in ()).throw(errors.control_plane_unavailable("transport.stop", 8.0)),
    )
    session.transport.responses["capture_stop"] = SimpleNamespace(
        ok=False, path=None, frames_written=None, err="Bridge wrote no audio frames"
    )

    with pytest.raises(errors.ToolError, match=r"transport\.stop was unacknowledged") as excinfo:
        controls._live_render(session, tmp_path / "mix.wav", 0.0)
    assert excinfo.value.category == "render_failed"


@pytest.mark.parametrize("wav", [None, b""])
def test_export_offline_raises_when_cli_wav_is_missing_or_empty(monkeypatch, tmp_path, wav):
    project = tmp_path / "live.flp"
    project.write_bytes(b"FLhd")

    def render(command, check, timeout, env=None):
        if wav is not None:
            project.with_suffix(".wav").write_bytes(wav)

    monkeypatch.setattr("fl_mcp.session.controls.subprocess.run", render)
    control = FakeControl(hello=HELLO, handlers={"ctl.save": lambda args: None})
    session, _ = make_session(control=control, project=project)

    with pytest.raises(errors.ToolError) as excinfo:
        session.export_offline(tmp_path / "mix.wav")
    assert excinfo.value.category == "render_failed"


def test_export_offline_maps_a_subprocess_timeout_to_render_failed(monkeypatch, tmp_path):
    project = tmp_path / "live.flp"
    project.write_bytes(b"FLhd")

    def render(command, check, timeout, env=None):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr("fl_mcp.session.controls.subprocess.run", render)
    control = FakeControl(hello=HELLO, handlers={"ctl.save": lambda args: None})
    session, _ = make_session(control=control, project=project)

    with pytest.raises(errors.ToolError) as excinfo:
        session.export_offline(tmp_path / "mix.wav")
    assert excinfo.value.category == "render_failed"


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
    assert seen_timeouts[-1] == controls.LIVE_SET_TIMEOUT_SECONDS


def test_save_project_retries_a_busy_control_wire(monkeypatch):
    attempts = 0

    def save(args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise errors.control_plane_busy("ctl.save", 8.0)
        return 8

    monkeypatch.setattr(controls.time, "sleep", lambda seconds: None)
    control = FakeControl(hello=HELLO, handlers={"ctl.save": save})
    session, _ = make_session(control=control)

    assert session.save_project() is None
    assert attempts == 2


def test_save_project_requires_control_plane():
    session, _ = make_session()

    with pytest.raises(errors.ToolError) as excinfo:
        session.save_project()
    assert excinfo.value.category == "control_plane_unavailable"
    assert f"within {controls.LIVE_SET_TIMEOUT_SECONDS:g}s" in excinfo.value.message


def test_set_tempo_live_unconfirmed_raises():
    control = FakeControl(hello=HELLO, handlers={"mixer.setCurrentTempo": lambda args: True})
    session, _ = make_session(control=control)

    with pytest.raises(WriteRejected, match="UNCONFIRMED"):
        session.set_tempo(130.0)


def test_set_tempo_project_backend(monkeypatch, tmp_path, no_reload):
    monkeypatch.setattr(flp, "set_tempo", lambda path, bpm: 137.0)
    session, _ = make_session(project=tmp_path / "agent.flp")

    assert session.set_tempo(137.0) == 137.0
    assert no_reload == [True]


def test_set_tempo_no_backend_raises():
    session, _ = make_session()
    with pytest.raises(NotePlaneUnavailable):
        session.set_tempo(120.0)


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
    assert ("channels.setChannelPitch", [2, 12, 1]) in control.calls


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


def test_levels_project_backend(monkeypatch, tmp_path, no_reload):
    from fl_mcp.schemas import ChannelLevels

    confirmed = ChannelLevels(channel=2, volume=0.6, pan=-0.3, pitch_semitones=0)
    monkeypatch.setattr(flp, "set_channel_levels", lambda *a, **k: confirmed)
    session, _ = make_session(project=tmp_path / "agent.flp")

    assert session.set_channel_levels(2, volume=0.6, pan=-0.3) == confirmed
    assert no_reload == [True]


def test_levels_require_at_least_one_field():
    session, _ = make_session(control=FakeControl(hello=HELLO))
    with pytest.raises(ValueError, match="nothing to change"):
        session.set_channel_levels(2)


# --- set_automation ---------------------------------------------------------


def test_automation_project_backend_returns_reparsed_points(monkeypatch, tmp_path, no_reload):
    saved = [flp.FlpAutomationPoint(position=1.0, value=0.25, tension=-0.5)]
    monkeypatch.setattr(flp, "write_automation", lambda *args: saved)
    monkeypatch.setattr(
        flp,
        "read",
        lambda _path: type(
            "Project", (), {"channels": [type("Channel", (), {"index": 2, "automation": saved})()]}
        )(),
    )
    session, _ = make_session(project=tmp_path / "agent.flp")

    receipt = session.set_automation(2, [AutomationPoint(position=1, value=0.25, tension=-0.5)])

    assert receipt.points == [AutomationPoint(position=1, value=0.25, tension=-0.5)]
    assert receipt.backend == "project-write"
    assert no_reload == [True]


def test_automation_refuses_a_session_reparse_mismatch(monkeypatch, tmp_path, no_reload):
    saved = [flp.FlpAutomationPoint(position=1.0, value=0.25, tension=-0.5)]
    reparsed = [flp.FlpAutomationPoint(position=1.0, value=0.5, tension=-0.5)]
    monkeypatch.setattr(flp, "write_automation", lambda *args: saved)
    monkeypatch.setattr(
        flp,
        "read",
        lambda _path: type(
            "Project", (), {"channels": [type("Channel", (), {"index": 2, "automation": reparsed})()]}
        )(),
    )
    session, _ = make_session(project=tmp_path / "agent.flp")

    with pytest.raises(WriteRejected, match="UNCONFIRMED"):
        session.set_automation(2, [AutomationPoint(position=1, value=0.25, tension=-0.5)])
    assert no_reload == []


def test_automation_file_seam_rejects_a_missing_readback(monkeypatch, tmp_path):
    monkeypatch.setattr(flp.flpkit, "write_automation", lambda *args: 1)
    monkeypatch.setattr(
        flp,
        "read",
        lambda _path: type(
            "Project", (), {"channels": [type("Channel", (), {"index": 2, "automation": None})()]}
        )(),
    )

    with pytest.raises(flp.FlpError, match="no automation readback"):
        flp.write_automation(tmp_path / "agent.flp", 2, [AutomationPoint(position=1, value=0.25)])


def test_automation_file_seam_rejects_an_absent_readback_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(flp.flpkit, "write_automation", lambda *args: 1)
    monkeypatch.setattr(
        flp,
        "read",
        lambda _path: type(
            "Project", (), {"channels": [type("Channel", (), {"index": 1, "automation": ()})()]}
        )(),
    )

    with pytest.raises(flp.FlpError, match="absent from the saved-file automation readback"):
        flp.write_automation(tmp_path / "agent.flp", 2, [AutomationPoint(position=1, value=0.25)])


def test_automation_requires_a_project_file():
    session, _ = make_session()
    with pytest.raises(NotePlaneUnavailable, match="No backend can write automation"):
        session.set_automation(2, [])


# --- play / stop / undo (addendum A11) ---------------------------------------


@pytest.mark.parametrize(
    ("method", "op"),
    [("play", "transport.start"), ("stop", "transport.stop"), ("undo", "general.undo")],
)
def test_transport_methods_send_their_op_and_return_none(method, op):
    control = FakeControl(hello=HELLO, handlers={op: lambda args: 0})
    session, _ = make_session(control=control)

    assert getattr(session, method)() is None
    assert control.calls == [(op, [])]


@pytest.mark.parametrize("method", ["play", "stop", "undo"])
def test_transport_methods_raise_without_a_control_plane(method):
    session, _ = make_session()
    with pytest.raises(errors.ToolError) as excinfo:
        getattr(session, method)()
    assert excinfo.value.category == "control_plane_unavailable"


def test_transport_unsafe_refusal_is_retryable_operation_unsafe():
    """Addendum A6: unsafe-at-current-time means busy, not broken."""
    control = FakeControl(hello=HELLO, handlers={"transport.start": lambda args: UNSAFE})
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as excinfo:
        session.play()
    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable is True


def test_transport_other_refusals_surface_fl_detail():
    boom = ControlResponse(id=1, ok=False, err="no such op")
    control = FakeControl(hello=HELLO, handlers={"general.undo": lambda args: boom})
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
        value, index, _ch, _slot = args
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
    assert ("plugins.setParamValue", [0.5, 2, 0, -1]) in control.calls


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

    assert ("plugins.setParamValue", [pytest.approx(420 / 980), 0, 0, -1]) in control.calls


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


def test_file_edit_batches_reloads_into_one(monkeypatch):
    # N file-lane reloads inside file_edit() collapse to a single FL reboot.
    from fl_mcp.session import lifecycle

    session, _ = make_session()
    reloads = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: reloads.append(1))

    with session.file_edit():
        session._reload_fl()
        session._reload_fl()
        session._reload_fl()
        assert reloads == [], "reloads must be deferred inside the batch"
    assert reloads == [1], "exactly one reload on batch exit"


def test_file_edit_saves_live_work_before_reloading(monkeypatch):
    from fl_mcp.session import lifecycle

    session, _ = make_session(control=FakeControl(hello=HELLO))
    events = []
    monkeypatch.setattr(controls, "save_project", lambda session: events.append("save"))
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: events.append("reload"))

    with session.file_edit():
        session._prepare_file_write()
        session._reload_fl()

    assert events == ["save", "reload"]


def test_file_edit_saves_once_before_its_single_reload(monkeypatch):
    from fl_mcp.session import lifecycle

    session, _ = make_session(control=FakeControl(hello=HELLO))
    events = []
    monkeypatch.setattr(controls, "save_project", lambda session: events.append("save"))
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: events.append("reload"))

    with session.file_edit():
        session._prepare_file_write()
        session._reload_fl()
        session._prepare_file_write()
        session._reload_fl()

    assert events == ["save", "reload"]


def test_reload_outside_a_batch_is_immediate(monkeypatch):
    from fl_mcp.session import lifecycle

    session, _ = make_session()
    reloads = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: reloads.append(1))
    session._reload_fl()
    session._reload_fl()
    assert reloads == [1, 1], "each reload is immediate when not batching"


def test_file_edit_with_no_file_ops_does_not_reload(monkeypatch):
    from fl_mcp.session import lifecycle

    session, _ = make_session()
    reloads = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: reloads.append(1))
    with session.file_edit():
        pass
    assert reloads == [], "an empty batch never reboots FL"


def test_session_file_edit_refuses_before_the_file_lane(monkeypatch, tmp_path):
    """Regression for live arenas: file_edit must never initiate a session reboot."""
    from fl_mcp.session import lifecycle

    session, _ = make_session(project=tmp_path / "agent.flp", session_spool_root=tmp_path)
    reloads: list[bool] = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda *args, **kwargs: reloads.append(True))

    with pytest.raises(errors.ToolError) as excinfo, session.file_edit():
        pass

    assert excinfo.value.category == "file_lane_requires_reload"
    assert excinfo.value.retryable is True
    assert reloads == []


def test_session_file_fallback_refuses_before_mutating_project(monkeypatch, tmp_path):
    """A failed live lane cannot turn into a hidden session file write/reboot."""
    project = tmp_path / "agent.flp"
    writes: list[bool] = []
    monkeypatch.setattr(flp, "set_tempo", lambda *args: writes.append(True))
    session, _ = make_session(project=project, session_spool_root=tmp_path)

    with pytest.raises(errors.ToolError) as excinfo:
        session.set_tempo(140)

    assert excinfo.value.category == "file_lane_requires_reload"
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


def test_render_child_never_inherits_the_live_session_identity(monkeypatch, tmp_path):
    """The -R render FL loads the Bridge too; it must publish into a throwaway spool, never ours."""
    from fl_mcp.contract import ENV_AGENT_PROJECT, ENV_SPOOL_ROOT

    project = tmp_path / "live.flp"
    project.write_bytes(b"FLhd")
    monkeypatch.setenv(ENV_SPOOL_ROOT, str(tmp_path / "live-spool"))
    monkeypatch.setenv(ENV_AGENT_PROJECT, str(project))
    seen: dict[str, object] = {}

    def render(command, check, timeout, env):
        seen["env"] = env
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr("fl_mcp.session.controls.subprocess.run", render)
    control = FakeControl(hello=HELLO, handlers={"ctl.save": lambda args: None})
    session, _ = make_session(control=control, project=project)

    with pytest.raises(errors.ToolError):
        session.export_offline(tmp_path / "mix.wav")

    env = seen["env"]
    assert isinstance(env, dict)
    assert env[ENV_SPOOL_ROOT] != str(tmp_path / "live-spool"), "the render child joined the live session"
    assert (
        env[ENV_SPOOL_ROOT].startswith(tempfile.gettempdir()) or "flmcp-render-spool-" in env[ENV_SPOOL_ROOT]
    )
    assert ENV_AGENT_PROJECT not in env
