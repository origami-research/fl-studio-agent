from __future__ import annotations

import pytest

from fake_fl import FakeBridge, FakeControl
from fl_mcp import errors, schemas
from fl_mcp.session import FLSession


@pytest.fixture
def wired(tmp_path):
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
    try:
        yield session, bridge, control
    finally:
        bridge.stop()


def test_write_notes_confirms_and_writes_through(wired):
    session, bridge, control = wired
    target = schemas.Target(pattern=1, channel=2)
    notes = [schemas.Note(key="C4", start=0, length=1)]

    resp, cmd = session.write_notes(target, notes, mode="replace")

    assert resp.ok
    assert resp.notes_written == 1
    assert bridge.notes_at(1, 2)
    ref = session.shadow.ref_at(target)
    assert ref is not None and ref.provenance == "shadow"
    last = session.actions.last()
    assert last.op == "add_notes"
    assert last.after_hash == ref.id
    assert last.undo_step is not None


def test_write_notes_failure_does_not_touch_shadow_or_actions(wired):
    session, bridge, control = wired
    bridge.fail_ops.add("add_notes")
    target = schemas.Target(pattern=1, channel=2)
    notes = [schemas.Note(key="C4", start=0, length=1)]

    resp, cmd = session.write_notes(target, notes, mode="replace")

    assert resp.ok is False
    assert session.shadow.ref_at(target) is None
    assert session.actions.last() is None


def test_write_notes_raises_tool_error_when_unclaimed(tmp_path):
    session = FLSession(project_key="test", spool_root=tmp_path / "spool", project_root=tmp_path / "project")
    target = schemas.Target(pattern=1, channel=2)
    notes = [schemas.Note(key="C4", start=0, length=1)]

    with pytest.raises(errors.ToolError) as exc_info:
        session.write_notes(target, notes, timeout=0.1)
    assert exc_info.value.category == "note_plane_unavailable"
    assert session.shadow.ref_at(target) is None


def test_clear_notes_records_before_and_after_hash(wired):
    session, bridge, control = wired
    target = schemas.Target(pattern=1, channel=2)
    session.write_notes(target, [schemas.Note(key="C4", start=0, length=1)], mode="replace")

    resp, cmd = session.clear_notes(target)

    assert resp.ok
    last = session.actions.last()
    assert last.op == "clear_notes"
    assert last.before_hash is not None
    assert last.after_hash is not None
    assert last.before_hash != last.after_hash


# -- doctor() ------------------------------------------------------------
#
# note_plane here reports the v0 interim path (docs/15 fix item 4 keeps notes
# on the pyscript path) — installed-or-not, not the v1 SpoolSession the fake
# bridge above drives. template_project is always "pending" (deferred product
# decision, this merge) — never a blocking WARN/FAIL.


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    from pathlib import Path

    settings = tmp_path / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return settings


def _install_note_script(settings):
    from fl_mcp.install import SCRIPT_FILENAME

    scripts_dir = settings / "Piano roll scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / SCRIPT_FILENAME).write_text("# fake install for tests")


def test_doctor_reports_connected_control_and_installed_note_plane(wired, fake_settings):
    _install_note_script(fake_settings)
    session, bridge, control = wired

    report = session.doctor()

    assert report["status"] == "ok"
    assert report["control_plane"]["connected"] is True
    assert report["control_plane"]["ppq"] == control.ppq
    assert report["note_plane"]["connected"] is True
    assert report["template_project"]["status"] == "pending"


def test_doctor_reports_degraded_when_note_plane_script_not_installed(wired, fake_settings):
    session, bridge, control = wired

    report = session.doctor()

    assert report["status"] == "degraded"
    assert report["note_plane"]["connected"] is False


def test_doctor_reports_not_configured_control_plane(tmp_path, fake_settings):
    session = FLSession(project_key="test", spool_root=tmp_path / "spool", project_root=tmp_path / "project")

    report = session.doctor()

    assert report["control_plane"]["connected"] is False
    assert report["status"] == "degraded"


def test_doctor_names_the_port_ordering_fix_when_fl_is_running_without_hello(tmp_path, fake_settings, monkeypatch):
    """docs/15 fix item 5: virtual ports must exist before FL launches. If FL's
    process is already up but never said hello, fl_doctor should say exactly
    that in plain words, not just 'not connected'."""
    from fl_mcp import session as session_module

    monkeypatch.setattr(session_module, "_fl_process_running", lambda: True)
    control = FakeControl()  # never sent hello - the port-ordering trap
    session = FLSession(
        project_key="test", spool_root=tmp_path / "spool", project_root=tmp_path / "project", control=control
    )

    report = session.doctor()

    assert report["control_plane"]["connected"] is False
    assert "restart FL Studio once" in report["control_plane"]["fix"]


def test_doctor_template_project_is_pending_not_blocking(wired, fake_settings):
    """A missing template must never read as a failure this wave (product
    decision: template placement is deferred until a licensed FL install)."""
    _install_note_script(fake_settings)
    session, bridge, control = wired

    report = session.doctor()

    assert report["status"] == "ok"
    assert report["template_project"] == {
        "status": "pending",
        "detail": "Deferred until a licensed FL Studio install is available. Not a failure.",
    }
