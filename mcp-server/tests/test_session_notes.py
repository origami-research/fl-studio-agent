"""Note routing on the file lane: readback receipts, honest failures.

The bridge lane's eligibility and behavior live in test_session_bridge.py;
here the heartbeat evidence is absent, so every targeted write is
project-write - and a connected control plane must NOT divert it (the
pyscript/spool lane is TARGET-BLIND, dogfood docs/21 A12).
"""

from __future__ import annotations

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
    make_session,
)

from fl_mcp import flp
from fl_mcp.schemas import Note
from fl_mcp.session import FLSession, NotePlaneUnavailable, WriteRejected


@pytest.fixture(autouse=True)
def isolated_spool(monkeypatch):
    isolate_spool(monkeypatch)


@pytest.fixture()
def no_reload(monkeypatch):
    """Never let a unit test SIGKILL or launch FL; record the calls instead."""
    calls: list[bool] = []
    monkeypatch.setattr(FLSession, "_reload_fl", lambda self: calls.append(True))
    return calls


def test_notes_go_project_write_even_when_control_is_connected(monkeypatch, tmp_path, no_reload):
    project = tmp_path / "agent.flp"
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, store = make_session(control=FakeControl(hello=HELLO), spool=spool, project=project)

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "project-write"
    assert spool.sent == [], "the target-blind pyscript lane must never see a targeted write"
    assert store.writes == [(TARGET, NOTES, "merge", "project-write", 0)]
    assert no_reload == [True]


def test_notes_without_a_project_raise_even_when_control_is_connected():
    session, store = make_session(control=FakeControl(hello=HELLO), project=None)

    with pytest.raises(NotePlaneUnavailable):
        session.write_notes(TARGET, NOTES, "merge")
    assert store.writes == []


def test_recording_failure_never_fails_an_applied_write(monkeypatch, tmp_path, no_reload, caplog):
    """Found live: the store crashed AFTER the file write landed, so every
    applied write reported a traceback. Recording is memory - it logs and
    yields an empty delta, and the receipt still reports the readback."""
    project = tmp_path / "agent.flp"
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    session, store = make_session(project=project)

    def boom(*a, **k):
        raise OSError("store root unwritable")

    store.record_write = boom
    with caplog.at_level("ERROR"):
        receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "project-write" and receipt.confirmed_notes == 1
    assert receipt.delta.is_empty()
    assert any("recording failed" in r.message for r in caplog.records)


def test_project_write_records_then_reloads(monkeypatch, tmp_path, no_reload):
    project = tmp_path / "agent.flp"
    seen: list[tuple] = []

    def fake_write(path, notes, *, pattern, channel, mode):
        seen.append((path, list(notes), pattern, channel, mode))
        return [object(), object()]  # the readback: 2 notes now in the file

    monkeypatch.setattr(flp, "write_notes", fake_write)
    session, store = make_session(project=project)

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "project-write"
    assert receipt.confirmed_notes == 2
    assert seen == [(project, NOTES, 1, 0, "merge")]
    assert store.writes == [(TARGET, NOTES, "merge", "project-write", 0)]
    assert no_reload == [True], "FL reloads exactly once, after the store recorded"


def test_project_write_readback_mismatch_raises(monkeypatch, tmp_path, no_reload):
    def failing_write(path, notes, *, pattern, channel, mode):
        raise flp.FlpError("readback mismatch at note 3")

    monkeypatch.setattr(flp, "write_notes", failing_write)
    session, store = make_session(project=tmp_path / "agent.flp")

    with pytest.raises(WriteRejected, match="did not verify"):
        session.write_notes(TARGET, NOTES, "merge")
    assert store.writes == []
    assert no_reload == [], "an unverified write must not reload FL"


def test_no_backend_raises_named_fix():
    session, _ = make_session()
    with pytest.raises(NotePlaneUnavailable) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")
    assert "make_agent_project" in excinfo.value.fix
    assert "FLMCP_AGENT_PROJECT" in excinfo.value.fix


def test_clear_notes_is_project_write_even_when_connected(monkeypatch, tmp_path, no_reload):
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: [])
    spool = FakeSpool()
    session, store = make_session(
        control=FakeControl(hello=HELLO), spool=spool, project=tmp_path / "agent.flp"
    )

    receipt = session.clear_notes(TARGET)

    assert receipt.backend == "project-write" and receipt.confirmed_notes == 0
    assert spool.sent == []


def test_read_notes_prefers_the_store(monkeypatch):
    store = FakeStore()
    store.preloaded[(1, 0)] = NOTES
    monkeypatch.setattr(
        flp, "notes_at", lambda *a: pytest.fail("must not touch the file when the store answers")
    )
    session, _ = make_session(store=store, project=Path("/tmp/agent.flp"))

    assert session.read_notes(TARGET) == NOTES


def test_read_notes_falls_back_to_file_and_marks_ground_truth(monkeypatch, tmp_path):
    project = tmp_path / "agent.flp"
    monkeypatch.setattr(
        flp,
        "read",
        lambda path: flp.FlpProject(ppq=96, tempo=None, channels=(), notes=()),
    )
    monkeypatch.setattr(
        flp,
        "notes_at",
        lambda path, pattern, channel: [
            flp.FlpNote(position=96, length=48, key=60, channel=0, velocity=127, pan=64)
        ],
    )
    session, store = make_session(project=project)

    notes = session.read_notes(TARGET)

    assert notes == [Note(key=60, start=1.0, length=0.5, velocity=1.0, pan=0.5)]
    assert store.ground_truths == [(TARGET, notes)]


def test_read_notes_with_no_source_raises():
    session, _ = make_session()
    with pytest.raises(NotePlaneUnavailable):
        session.read_notes(TARGET)
