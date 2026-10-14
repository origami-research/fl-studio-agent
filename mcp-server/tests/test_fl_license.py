"""FLMCP_FL_LICENSE=trial: nothing may make a trial FL reopen a saved project.

A trial FL cannot reopen a project it saved. The file lane (write the .flp,
then kill and relaunch FL on it) and CLI export (a second FL opening the saved
.flp) would therefore strand or kill the only FL on a trial box. These tests
pin the invariant at its one choke point: refused before any file mutation,
reload, or subprocess.
"""

from __future__ import annotations

import pytest
from session_fakes import HELLO, FakeControl, make_session

from fl_mcp import errors, flp
from fl_mcp.contract import ENV_FL_LICENSE, FLLicense, build_contract, fl_license_from_env
from fl_mcp.schemas import AutomationPoint
from fl_mcp.session import lifecycle


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


def test_trial_refuses_a_save_before_asking_fl():
    saves: list[object] = []
    control = FakeControl(hello=HELLO, handlers={"ctl.save": saves.append})
    session, _ = make_session(control=control, fl_license=FLLicense.TRIAL)
    with pytest.raises(errors.ToolError) as excinfo:
        session.save_project()
    assert excinfo.value.category == "fl_trial_cannot_save"
    assert excinfo.value.retryable is False
    assert saves == []
