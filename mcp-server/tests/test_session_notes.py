"""Note routing on the file lane: readback receipts, honest failures.

The bridge lane's eligibility and behavior live in test_session_bridge.py.
Notes need a live Bridge, so here one ticks but no wire can verify its target
channel: every targeted write is project-write - and a connected control plane
must NOT divert it (the pyscript/spool lane is TARGET-BLIND, dogfood docs/21 A12).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from session_fakes import (
    HELLO,
    NOTES,
    TARGET,
    FakeControl,
    FakeSpool,
    FakeStore,
    isolate_spool,
    live_untargeted_bridge,
    make_session,
)

from fl_mcp import errors, flp
from fl_mcp.schemas import Note, WriteResult
from fl_mcp.session import FLSession, NotePlaneUnavailable, WriteRejected
from fl_mcp.template import template_path


@pytest.fixture(autouse=True)
def bridge_ticking(monkeypatch, tmp_path):
    live_untargeted_bridge(monkeypatch, tmp_path)


@pytest.fixture()
def no_reload(monkeypatch):
    """Never let a unit test SIGKILL or launch FL; record the calls instead."""
    calls: list[bool] = []
    monkeypatch.setattr(FLSession, "_reload_fl", lambda self: calls.append(True))
    return calls


@pytest.fixture()
def project(tmp_path) -> Path:
    """A real copy of the session template (5 channels, no notes): the file
    lane's surgery and readback run unpatched."""
    path = tmp_path / "agent.flp"
    shutil.copyfile(template_path(), path)
    return path


def keys(notes) -> list[tuple[int, float]]:
    return [(n.key, n.start) for n in notes]


def test_notes_go_project_write_even_when_control_is_connected(project, no_reload):
    spool = FakeSpool()
    session, store = make_session(control=FakeControl(hello=HELLO), transport=spool, project=project)

    result = session.write_notes(TARGET, NOTES, "merge")

    assert result == WriteResult(wrote=1, track="808 Kick", pattern=1, mode="merge", total=1)
    assert spool.sent == [], "the target-blind pyscript lane must never see a targeted write"
    (write,) = store.writes
    assert write[0] == TARGET and write[3:] == ("merge", "project-write", 0)
    assert keys(write[2]) == keys(NOTES), "the recorded state is the file's readback"
    assert no_reload == [True]


def test_notes_without_a_project_raise_even_when_control_is_connected(no_reload):
    """The Bridge ticks but cannot target and there is no file to fall back on."""
    session, store = make_session(control=FakeControl(hello=HELLO), project=None)

    with pytest.raises(errors.ToolError):
        session.write_notes(TARGET, NOTES, "merge")
    assert store.writes == []
    assert no_reload == []


def test_recording_failure_never_fails_an_applied_write(project, no_reload, caplog):
    """Found live: the store crashed AFTER the file write landed, so every
    applied write reported a traceback. The action log is history - its
    failure logs, and the result still reports the readback."""
    session, store = make_session(project=project)

    def boom(*a, **k):
        raise OSError("store root unwritable")

    store.record_write = boom
    with caplog.at_level("ERROR"):
        result = session.write_notes(TARGET, NOTES, "merge")

    assert result.total == 1
    assert any("action-log append failed" in r.message for r in caplog.records)


def test_project_write_records_the_readback_then_reloads(project, no_reload):
    session, store = make_session(project=project)
    session.write_notes(TARGET, NOTES, "replace")

    result = session.write_notes(TARGET, [Note(key=64, start=1, length=1)], "merge")

    assert result.total == 2
    before, after = store.writes[1][1], store.writes[1][2]
    assert keys(before) == [(60, 0)] and keys(after) == [(60, 0), (64, 1)]
    assert keys(session.read_notes(TARGET)) == [(60, 0), (64, 1)]
    assert no_reload == [True, True], "FL reloads once per write, after the store recorded"


def test_project_write_saves_live_state_before_writing_and_reloading(monkeypatch, project):
    from fl_mcp.session import controls, lifecycle

    events = []
    control = FakeControl(hello=HELLO)
    session, _ = make_session(control=control, project=project)
    monkeypatch.setattr(controls, "save_project", lambda session: events.append("save"))
    monkeypatch.setattr(flp, "write_notes", lambda *args, **kwargs: events.append("write") or [])
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: events.append("reload"))

    session.write_notes(TARGET, NOTES, "merge")

    assert events == ["save", "write", "reload"]


def test_project_write_skips_save_without_live_control(monkeypatch, project):
    from fl_mcp.session import controls, lifecycle

    events = []
    session, _ = make_session(project=project)
    monkeypatch.setattr(controls, "save_project", lambda session: events.append("save"))
    monkeypatch.setattr(flp, "write_notes", lambda *args, **kwargs: events.append("write") or [])
    monkeypatch.setattr(lifecycle, "reload_fl", lambda project, link: events.append("reload"))

    session.write_notes(TARGET, NOTES, "merge")

    assert events == ["write", "reload"]


def test_project_write_readback_mismatch_raises(monkeypatch, project, no_reload):
    def failing_write(path, notes, *, pattern, channel, mode):
        raise flp.FlpError("readback mismatch at note 3")

    monkeypatch.setattr(flp, "write_notes", failing_write)
    session, store = make_session(project=project)

    with pytest.raises(WriteRejected, match="could not be written"):
        session.write_notes(TARGET, NOTES, "merge")
    assert store.writes == []
    assert no_reload == [], "an unverified write must not reload FL"


def test_no_live_bridge_refuses_plainly_and_logs_why(monkeypatch, project, no_reload, caplog):
    """The Bridge is required: with none ticking (FL on another song, or none
    installed) a write must not save FL's open song, rewrite the file and relaunch
    FL, as the old file fallback did. It refuses and says how to fix it."""
    isolate_spool(monkeypatch)
    writes: list[bool] = []
    monkeypatch.setattr(flp, "write_notes", lambda *args, **kwargs: writes.append(True))
    control = FakeControl(hello=HELLO)
    session, store = make_session(control=control, project=project)

    with caplog.at_level("WARNING"), pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")

    assert excinfo.value.category == "note_plane_unavailable"
    assert "song prepared for you" in excinfo.value.message
    assert "open the FLMCP Agent song" in excinfo.value.fix
    assert any("no live Bridge" in r.message for r in caplog.records)
    assert writes == [] and store.writes == [] and no_reload == []
    assert "ctl.save" not in [op for op, _ in control.calls]


def test_concurrent_session_write_raises_retryable_error(tmp_path):
    session, _ = make_session(project=tmp_path / "agent.flp")
    session._write_lock.acquire()
    try:
        with pytest.raises(errors.ConcurrentWrite) as excinfo:
            session.write_notes(TARGET, NOTES, "merge")
    finally:
        session._write_lock.release()
    assert excinfo.value.category == "concurrent_write"
    assert excinfo.value.retryable is True


def test_clear_notes_is_project_write_even_when_connected(project, no_reload):
    spool = FakeSpool()
    session, _ = make_session(control=FakeControl(hello=HELLO), transport=spool, project=project)
    session.write_notes(TARGET, NOTES, "replace")

    result = session.clear_notes(TARGET)

    assert result == WriteResult(wrote=0, track="808 Kick", pattern=1, mode="replace", total=0)
    assert session.read_notes(TARGET) == []
    assert spool.sent == []


def test_read_notes_prefers_the_store(monkeypatch, project):
    store = FakeStore()
    store.record_write(TARGET, [], NOTES, "replace", "bridge", 1)
    monkeypatch.setattr(
        flp, "notes_at", lambda *a: pytest.fail("must not touch the file when the store answers")
    )
    session, _ = make_session(store=store, project=project)

    assert session.read_notes(TARGET) == NOTES


def _one_file_note(monkeypatch, velocity: int) -> None:
    monkeypatch.setattr(
        flp,
        "read",
        lambda path: flp.FlpProject(ppq=96, tempo=None, channels=(), notes=()),
    )
    monkeypatch.setattr(
        flp,
        "notes_at",
        lambda path, pattern, channel: [
            flp.FlpNote(position=96, length=48, key=60, channel=0, velocity=velocity, pan=64)
        ],
    )


def test_an_unwritten_target_reads_the_project_every_time(monkeypatch, project):
    """No caching: the store holds only this session's writes, so a project
    re-read (after FL saved, say) is never shadowed by an old read."""
    _one_file_note(monkeypatch, 127)
    session, store = make_session(project=project)

    notes = session.read_notes(TARGET)

    assert notes == [Note(key=60, start=1.0, length=0.5, velocity=1.0, pan=0.5)]
    assert store.writes == [] and store.written() == {}


def test_step_notes_read_as_fl_has_them(monkeypatch, project):
    """FL stores a step-sequencer hit as a length-0 note; users' projects are full of
    them, so reading one must not fail validation (live: get_scene and check both
    failed on a user's project, 2026-10-23)."""
    monkeypatch.setattr(flp, "read", lambda path: flp.FlpProject(ppq=96, tempo=None, channels=(), notes=()))
    monkeypatch.setattr(
        flp,
        "notes_at",
        lambda path, pattern, channel: [
            flp.FlpNote(position=96, length=0, key=36, channel=0, velocity=100, pan=64)
        ],
    )
    session, _store = make_session(project=project)

    (note,) = session.read_notes(TARGET)

    assert (note.key, note.start, note.length) == (36, 1.0, 0.0)


def test_read_notes_clamps_fl_raw_velocity_128(monkeypatch, project):
    """FL's 128 byte value decodes to the SDK's full-scale unit value."""
    _one_file_note(monkeypatch, 128)
    session, _store = make_session(project=project)

    assert session.read_notes(TARGET)[0].velocity == 1.0


def test_read_notes_with_no_source_raises():
    session, _ = make_session()
    with pytest.raises(NotePlaneUnavailable):
        session.read_notes(TARGET)
