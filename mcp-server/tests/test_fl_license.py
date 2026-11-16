"""FLMCP_FL_LICENSE=trial: nothing may make a trial FL reopen a saved project.

A trial FL cannot reopen a project it saved. The file lane (write the .flp,
then kill and relaunch FL on it) would therefore strand or kill the only FL on
a trial box. These tests pin the invariant at its one choke point: refused
before any file mutation or reload.
"""

from __future__ import annotations

import pytest
from session_fakes import HELLO, FakeControl, make_session

from fl_mcp import errors, flp
from fl_mcp.contract import ENV_FL_LICENSE, FLLicense, fl_license_from_env
from fl_mcp.session import lifecycle


def test_license_defaults_to_licensed_and_rejects_unknown_values():
    assert fl_license_from_env({}) is FLLicense.LICENSED
    assert fl_license_from_env({ENV_FL_LICENSE: "trial"}) is FLLicense.TRIAL
    with pytest.raises(ValueError, match="licensed, trial"):
        fl_license_from_env({ENV_FL_LICENSE: "demo"})


@pytest.fixture()
def trial(monkeypatch, tmp_path):
    """A trial session whose project file, flpkit writers and reload all record instead
    of acting, so a refusal can prove nothing happened."""
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    touched: list[str] = []
    monkeypatch.setattr(lifecycle, "reload_fl", lambda *args, **kwargs: touched.append("reload"))
    monkeypatch.setattr(flp, "set_tempo", lambda *args, **kwargs: touched.append("flp.set_tempo"))
    session, _ = make_session(project=project, fl_license=FLLicense.TRIAL)
    return session, touched, tmp_path


@pytest.mark.parametrize(
    "call",
    [lambda session: session._reload_fl(), lambda session: session._prepare_file_write()],
    ids=["reload", "file-write"],
)
def test_trial_refuses_every_reopen_path_before_acting(trial, call):
    session, touched, _ = trial
    with pytest.raises(errors.ToolError) as excinfo:
        call(session)
    assert excinfo.value.category == "fl_trial_unsupported"
    assert excinfo.value.retryable is False
    assert touched == []


def test_a_tempo_fl_does_not_answer_never_reaches_the_saved_song(trial):
    session, touched, _ = trial
    with pytest.raises(errors.ToolError) as excinfo:
        session.set_tempo(140)
    assert excinfo.value.category == "control_plane_unavailable"
    assert touched == []


def test_trial_refuses_a_save_before_asking_fl():
    saves: list[object] = []
    control = FakeControl(hello=HELLO, handlers={"ctl.save": saves.append})
    session, _ = make_session(control=control, fl_license=FLLicense.TRIAL)
    with pytest.raises(errors.ToolError) as excinfo:
        session.save_project()
    assert excinfo.value.category == "fl_trial_unsupported"
    assert excinfo.value.retryable is False
    assert saves == []
