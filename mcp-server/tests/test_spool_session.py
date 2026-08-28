from __future__ import annotations

import time

import pytest
from fake_fl import FakeBridge
from pydantic import ValidationError

from fl_mcp import errors, spool


@pytest.fixture
def session(tmp_path):
    s = spool.SpoolSession(root=tmp_path)
    yield s


def test_send_command_writes_a_sequenced_file(session):
    cmd = session.send_command("ping")
    assert cmd.id == 1
    path = session.commands_dir / "000001.json"
    assert path.is_file()
    payload = spool.read_json(path)
    assert payload["op"] == "ping"
    assert payload["id"] == 1


def test_sequence_numbers_increase_monotonically(session):
    ids = [session.send_command("ping").id for _ in range(5)]
    assert ids == [1, 2, 3, 4, 5]


def test_writes_are_atomic_json(session, tmp_path):
    session.send_command("ping")
    # No .tmp files left behind after a write.
    leftovers = list(session.commands_dir.glob("*.tmp"))
    assert leftovers == []


def test_unclaimed_command_times_out_with_note_plane_error(session):
    cmd = session.send_command("ping")
    start = time.monotonic()
    with pytest.raises(errors.ToolError) as exc_info:
        session.wait_for_response(cmd.id, timeout=0.15)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "must not block past the requested timeout"
    assert exc_info.value.category == "note_plane_unavailable"
    assert exc_info.value.retryable is True
    assert "Bridge" in exc_info.value.fix


def test_bridge_claims_and_responds(session):
    bridge = FakeBridge(session)
    bridge.start()
    try:
        cmd = session.send_command(
            "add_notes",
            target={"pattern": 1, "channel": 2},
            notes=[{"key": "C4", "start": 0, "length": 1}],
        )
        resp = session.wait_for_response(cmd.id, timeout=1.0)
        assert resp.ok
        assert resp.notes_written == 1
        assert resp.plugin_ver
        # Command file moved to done/, not left pending.
        assert not (session.commands_dir / "000001.json").is_file()
        assert (session.done_dir / "000001.json").is_file()
    finally:
        bridge.stop()


def test_response_already_present_returns_immediately(session):
    cmd = session.send_command("ping")
    spool.write_json_atomic(session.responses_dir / f"{cmd.id:06d}.json", {"id": cmd.id, "ok": True})
    resp = session.wait_for_response(cmd.id, timeout=0.05)
    assert resp.ok


def test_resuming_a_session_continues_the_counter(tmp_path):
    s1 = spool.SpoolSession(root=tmp_path)
    s1.send_command("ping")
    s1.send_command("ping")

    s2 = spool.SpoolSession(root=tmp_path, session_id=s1.session_id)
    cmd = s2.send_command("ping")
    assert cmd.id == 3


def test_gc_stale_sessions_removes_old_dirs_only(tmp_path):
    fresh = spool.SpoolSession(root=tmp_path)
    stale = spool.SpoolSession(root=tmp_path)
    # Backdate the stale session's heartbeat.
    stale_payload = spool.read_json(stale.dir / "session.json")
    stale_payload["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
    spool.write_json_atomic(stale.dir / "session.json", stale_payload)

    removed = spool.gc_stale_sessions(root=tmp_path, max_age=3600)

    assert stale.session_id in removed
    assert fresh.session_id not in removed
    assert not stale.dir.exists()
    assert fresh.dir.exists()


def test_send_command_validates_args_against_schema(session):
    with pytest.raises(ValidationError):
        session.send_command("add_notes", target={"pattern": 0, "channel": 0}, notes=[])


def test_next_command_id_unique_under_concurrency(tmp_path, monkeypatch):
    """Concurrent tool calls must never mint the same command id.

    Duplicate ids correlate the wrong FL response to the wrong command, so the
    read-modify-write of the counter file has to be serialized.
    """
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(spool, "spool_root", lambda: tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: spool.next_command_id(), range(200)))

    assert len(set(ids)) == len(ids)
    assert max(ids) == len(ids)
