from __future__ import annotations

from pathlib import Path

import pytest

from fake_fl import FakeBridge, FakeControl
from fl_mcp import errors, server
from fl_mcp.session import FLSession


@pytest.fixture
def wired_session(tmp_path):
    control = FakeControl()
    session = FLSession(
        project_key="test",
        spool_root=tmp_path / "spool",
        project_root=tmp_path / "project",
        control=control,
    )
    control.send_hello()
    bridge = FakeBridge(session.spool)
    bridge.start()
    server.set_session(session)
    try:
        yield session, bridge, control
    finally:
        bridge.stop()
        server.set_session(None)


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    settings = tmp_path / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return settings


def _install_note_script(settings):
    from fl_mcp.install import SCRIPT_FILENAME

    scripts_dir = settings / "Piano roll scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / SCRIPT_FILENAME).write_text("# fake install for tests")


# --- write_notes / clear_notes v1 (explicit target) -------------------------


def test_write_notes_with_target_blocks_for_real_confirmation(wired_session):
    session, bridge, control = wired_session
    result = server.write_notes(
        [{"key": "C4", "start": 0, "length": 1}],
        pattern=3,
        channel=4,
    )
    assert result["status"] == "ok"
    assert result["notes_written"] == 1
    assert result["target"] == {"pattern": 3, "channel": 4}
    assert bridge.notes_at(3, 4)


def test_write_notes_with_target_reports_structured_error_when_bridge_down(wired_session):
    session, bridge, control = wired_session
    bridge.stop()

    def raise_unavailable(*a, **kw):
        raise errors.note_plane_unavailable(999, 0.05)

    session.write_notes = raise_unavailable  # type: ignore[method-assign]
    result = server.write_notes([{"key": "C4", "start": 0, "length": 1}], pattern=1, channel=1)
    assert result["status"] == "error"
    assert result["error"] == "note_plane_unavailable"
    assert result["retryable"] is True


def test_write_notes_with_target_surfaces_plugin_side_failure(wired_session):
    session, bridge, control = wired_session
    bridge.fail_ops.add("add_notes")
    result = server.write_notes([{"key": "C4", "start": 0, "length": 1}], pattern=1, channel=1)
    assert result["status"] == "error"
    assert result["error"] == "fl_api_error"


def test_clear_notes_with_target_confirms(wired_session):
    session, bridge, control = wired_session
    server.write_notes([{"key": "C4", "start": 0, "length": 1}], pattern=1, channel=1)
    result = server.clear_notes(pattern=1, channel=1)
    assert result["status"] == "ok"
    assert result["notes_cleared"] == 1
    assert bridge.notes_at(1, 1) == []


def test_write_notes_validation_error_short_circuits_before_any_plane(wired_session):
    result = server.write_notes([{"key": "nope", "start": 0, "length": 1}], pattern=1, channel=1)
    assert result["status"] == "error"
    assert "note 0" in result["error"]


def test_write_notes_bad_mode_short_circuits(wired_session):
    result = server.write_notes([{"key": "C4", "start": 0, "length": 1}], mode="bogus", pattern=1, channel=1)
    assert result["status"] == "error"


# --- v0 path stays exactly as before, even with the new signature -----------


def test_write_notes_default_target_still_uses_v0_queued_workflow(tmp_path, monkeypatch):
    settings = tmp_path / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    (settings / "Piano roll scripts").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = server.write_notes([{"key": "C4", "start": 0, "length": 1}])

    assert result["status"] == "queued"
    assert "next_step" in result
    assert "target" not in result


# --- get_notes ---------------------------------------------------------------


def test_get_notes_returns_empty_with_provenance_before_any_write(wired_session):
    result = server.get_notes(pattern=5, channel=6)
    assert result["status"] == "ok"
    assert result["source"] == "shadow"
    assert result["notes"] == []


def test_get_notes_reflects_a_prior_write_with_provenance_and_staleness(wired_session):
    server.write_notes([{"key": "C4", "start": 0, "length": 1}], pattern=1, channel=1)
    result = server.get_notes(pattern=1, channel=1)
    assert result["source"] == "shadow"
    assert result["stale"] is False
    assert len(result["notes"]) == 1
    assert result["notes"][0]["key"] == 60


def test_get_notes_filters_by_key_range(wired_session):
    server.write_notes(
        [{"key": "C4", "start": 0, "length": 1}, {"key": "C6", "start": 1, "length": 1}],
        mode="replace",
        pattern=1,
        channel=1,
    )
    result = server.get_notes(pattern=1, channel=1, key_min=59, key_max=61)
    assert [n["key"] for n in result["notes"]] == [60]


# --- get_session_state --------------------------------------------------------


def test_get_session_state_without_control_plane_is_honest(tmp_path):
    session = FLSession(
        project_key="nocontrol", spool_root=tmp_path / "spool", project_root=tmp_path / "project"
    )
    server.set_session(session)
    try:
        result = server.get_session_state()
        assert result["status"] == "error"
        assert result["error"] == "control_plane_unavailable"
    finally:
        server.set_session(None)


def test_get_session_state_reports_the_snapshot(wired_session):
    session, bridge, control = wired_session
    result = server.get_session_state()
    assert result["status"] == "ok"
    assert result["ppq"] == control.ppq
    assert "channels" in result and "mixer" in result


# --- fl_doctor ----------------------------------------------------------------
#
# note_plane reflects the v0 interim path (docs/15 fix item 4), not the fake
# bridge's v1 SpoolSession — see test_session.py's doctor() tests for the same
# contract at the session layer.


def test_fl_doctor_ok_when_control_connected_and_note_script_installed(wired_session, fake_settings):
    _install_note_script(fake_settings)
    result = server.fl_doctor()
    assert result["status"] == "ok"
    assert result["control_plane"]["connected"] is True
    assert result["note_plane"]["connected"] is True
    assert result["template_project"]["status"] == "pending"


def test_fl_doctor_degraded_when_note_script_not_installed(wired_session, fake_settings):
    result = server.fl_doctor()
    assert result["status"] == "degraded"
    assert result["note_plane"]["connected"] is False


# --- undo_last -----------------------------------------------------------------


def test_undo_last_with_no_history_names_the_problem(wired_session):
    result = server.undo_last()
    assert result["status"] == "error"
    assert result["error"] == "nothing to undo"


def test_undo_last_identifies_but_does_not_apply(wired_session):
    server.write_notes([{"key": "C4", "start": 0, "length": 1}], pattern=1, channel=1)
    result = server.undo_last()
    assert result["status"] == "not_implemented"
    assert result["would_undo"]["op"] == "add_notes"
