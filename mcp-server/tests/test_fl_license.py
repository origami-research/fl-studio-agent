"""FLMCP_FL_LICENSE=trial: nothing may make a trial FL reopen a saved project.

A trial FL cannot reopen a project it saved. The file lane (write the .flp,
then kill and relaunch FL on it) and CLI export (a second FL opening the saved
.flp) would therefore strand or kill the only FL on a trial box. These tests
pin the invariant at its one choke point: refused before any file mutation,
reload, or subprocess.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from session_fakes import HELLO, FakeControl, FakeSpool, make_session

from fl_mcp import errors, flp
from fl_mcp.contract import ENV_FL_LICENSE, FLLicense, build_contract, fl_license_from_env
from fl_mcp.schemas import AutomationPoint, ControlResponse, SpoolResponse
from fl_mcp.session import FLSession, controls, lifecycle


def test_license_defaults_to_licensed_and_rejects_unknown_values():
    assert fl_license_from_env({}) is FLLicense.LICENSED
    assert fl_license_from_env({ENV_FL_LICENSE: "trial"}) is FLLicense.TRIAL
    with pytest.raises(ValueError, match="licensed, trial"):
        fl_license_from_env({ENV_FL_LICENSE: "demo"})


def test_the_contract_publishes_the_license_vocabulary():
    assert build_contract()["env"][ENV_FL_LICENSE] == ["licensed", "trial"]


@pytest.fixture()
def trial(monkeypatch, tmp_path):
    """A trial session whose project file, flpkit writers, reload, and subprocess
    all record instead of acting, so a refusal can prove nothing happened."""
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    touched: list[str] = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda *args, **kwargs: touched.append("reload"))
    monkeypatch.setattr(flp, "set_tempo", lambda *args, **kwargs: touched.append("flp.set_tempo"))
    monkeypatch.setattr(flp, "write_automation", lambda *args, **kwargs: touched.append("flp.write_automation"))
    monkeypatch.setattr(flp, "add_effect", lambda *args, **kwargs: touched.append("flp.add_effect"))
    monkeypatch.setattr(
        "fl_mcp.session.controls.subprocess.run", lambda *args, **kwargs: touched.append("subprocess")
    )
    session, _ = make_session(project=project, fl_license=FLLicense.TRIAL)
    return session, touched, tmp_path


@pytest.mark.parametrize(
    "call",
    [
        lambda session, _tmp: session.set_tempo(140),  # no control wire: the file fallback
        lambda session, _tmp: session.set_automation(0, [AutomationPoint(position=0, value=0.5)]),
        lambda session, _tmp: session.add_effect(0, "Fruity Limiter"),
        lambda session, _tmp: session._reload_fl(),
        lambda session, tmp: session.export_offline(tmp / "mix.wav"),
    ],
    ids=["tempo-file-fallback", "automation", "add-effect", "reload", "export-offline"],
)
def test_trial_refuses_every_reopen_path_before_acting(trial, call):
    session, touched, tmp_path = trial
    with pytest.raises(errors.ToolError) as excinfo:
        call(session, tmp_path)
    assert excinfo.value.category == "fl_trial_cannot_reopen"
    assert excinfo.value.retryable is False
    assert touched == []


def test_trial_still_serves_live_lanes(monkeypatch, tmp_path):
    """The switch removes reopen paths only; the control wire keeps working."""
    readback = {"tempo": 120_000}

    def set_tempo(args):
        readback["tempo"] = round(args[0] * 1000)

    control = FakeControl(
        hello=HELLO,
        handlers={"mixer.setCurrentTempo": set_tempo, "mixer.getCurrentTempo": lambda args: readback["tempo"]},
    )
    session, _ = make_session(control=control, project=tmp_path / "agent.flp", fl_license=FLLicense.TRIAL)
    assert session.set_tempo(140) == 140.0


def _render_rig(monkeypatch, tmp_path, *, fl_license, save):
    """A verified Bridge capture whose only variable is the pre-render save."""
    captured = tmp_path / "capture.wav"
    captured.write_bytes(b"RIFF" + b"\0" * 45)
    transport = FakeSpool(
        responses={
            "capture_start": SpoolResponse(id=1, ok=True, path=str(captured), frames_requested=100, sample_rate=44100),
            "capture_stop": SpoolResponse(id=2, ok=True, path=str(captured), frames_written=100, sample_rate=44100),
        }
    )
    control = FakeControl(
        hello=HELLO,
        handlers={
            "ctl.save": save,
            "transport.setSongPos": lambda args: None,
            "transport.start": lambda args: None,
            "transport.stop": lambda args: None,
        },
    )
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    session, _ = make_session(control=control, transport=transport, project=project, fl_license=fl_license)
    monkeypatch.setattr(FLSession, "caps", property(lambda self: SimpleNamespace(bridge_live=True)))
    monkeypatch.setattr(controls, "_render_span_seconds", lambda project: 0.0)
    monkeypatch.setattr(controls.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(controls.audio, "_read_wav", lambda path: ([0.1] * 100, 44100))
    monkeypatch.setattr(controls.audio, "analyze_wav", lambda path: SimpleNamespace(integrated_lufs=-12.0))
    return session, transport


def _buy_dialog(args):
    return ControlResponse(id=0, ok=False, err="save blocked by the Buy FL Studio dialog")


def test_trial_render_never_asks_fl_to_save(monkeypatch, tmp_path):
    """Live box 2026-10-05: a trial save opened FL's modal Buy dialog and the
    render's transport commands then timed out. Under trial the render records
    through the Bridge without a save, and the receipt says so."""
    saves: list[object] = []
    session, transport = _render_rig(monkeypatch, tmp_path, fl_license=FLLicense.TRIAL, save=saves.append)

    receipt = session.render_wav(tmp_path / "mix.wav")

    assert saves == []
    assert receipt.path == (tmp_path / "mix.wav").resolve()
    assert receipt.path.read_bytes()[:4] == b"RIFF"
    assert receipt.project_saved is False
    assert "Buy dialog" in receipt.project_save_detail
    assert [op for op, _ in transport.sent] == ["capture_start", "capture_stop"]


def test_trial_refuses_a_save_before_asking_fl():
    saves: list[object] = []
    control = FakeControl(hello=HELLO, handlers={"ctl.save": saves.append})
    session, _ = make_session(control=control, fl_license=FLLicense.TRIAL)
    with pytest.raises(errors.ToolError) as excinfo:
        session.save_project()
    assert excinfo.value.category == "fl_trial_cannot_save"
    assert excinfo.value.retryable is False
    assert saves == []


def test_licensed_render_still_fails_on_a_failed_save(monkeypatch, tmp_path):
    """Licensed behavior is unchanged: no capture starts after a refused save."""
    session, transport = _render_rig(monkeypatch, tmp_path, fl_license=FLLicense.LICENSED, save=_buy_dialog)
    with pytest.raises(errors.ToolError) as excinfo:
        session.render_wav(tmp_path / "mix.wav")
    assert excinfo.value.category == "fl_api_error"
    assert transport.sent == []
