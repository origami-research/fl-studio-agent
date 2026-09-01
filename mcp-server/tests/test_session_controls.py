"""Live controls: tempo, channel levels, transport, plugin params.

One backend per call, readback results, and the unsafe-at-current-time
refusal is retryable - never a fallthrough (addenda A6/A11).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from session_fakes import HELLO, FakeControl, make_session

from fl_mcp import errors, flp
from fl_mcp.schemas import AutomationPoint, ControlResponse, PluginParamModel, PluginParamSpec
from fl_mcp.session import FLSession, NotePlaneUnavailable, WriteRejected


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


def test_render_wav_uses_control_lane_and_confirms_artifact(tmp_path):
    output = tmp_path / "mix.wav"

    def export(args):
        Path(args[0]).write_bytes(b"RIFF....WAVE")
        return None

    control = FakeControl(hello=HELLO, handlers={"ui.exportWav": export})
    session, _ = make_session(control=control)

    assert session.render_wav(output) == output.resolve()
    assert ("ui.exportWav", [str(output.resolve())]) in control.calls


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
    monkeypatch.setattr(
        flp, "set_tempo", lambda *a: pytest.fail("must not fall through to the file backend")
    )
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


def test_levels_live_partial_failure_raises_and_never_falls_through(
    monkeypatch, tmp_path, no_reload
):
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
        lambda _path: type("Project", (), {"channels": [type("Channel", (), {"index": 2, "automation": saved})()]})(),
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
    session, _ = make_session(
        control=_param_control({0: 0.0}), plugin_param_models=(model,)
    )

    confirmed = session.set_plugin_param(0, 0, "On")
    assert (confirmed.kind, confirmed.value, confirmed.display) == ("bool", 1.0, "On")


def test_plugin_params_without_a_wire_is_an_honest_error():
    """Plugin state has no file representation - live-or-error, no fallback."""
    session, _ = make_session(control=None)
    with pytest.raises(errors.ToolError) as excinfo:
        session.plugin_params(0)
    assert excinfo.value.category == "control_plane_unavailable"
