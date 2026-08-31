"""SpoolCarrier: the file-mailbox framing against a tmp_path root.

The consumer side (the piano-roll pyscript) is exercised separately in
test_pyscript.py under the FL sandbox emulation; here we prove the carrier
half: atomic sequenced files, queue upkeep, honest timeouts, tombstoned
cancellations. Validation and id allocation belong to Transport
(test_transport.py) - a carrier only moves already-built commands.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

from fl_mcp.core.carriers.spool import (
    SpoolCarrier,
    SpoolUnavailable,
    read_json,
    resume_next_id,
    spool_roots,
    write_json_atomic,
)
from fl_mcp.errors import ToolError
from fl_mcp.schemas import SpoolCommand


def make_carrier(tmp_path) -> tuple[SpoolCarrier, Path]:
    root = tmp_path / "spoolroot"
    return SpoolCarrier(root), root


def queue_ids(root) -> list[int]:
    payload = read_json(root / "commands" / "queue.json")
    return (payload or {}).get("ids", [])


CMD = SpoolCommand(id=1, op="add_notes", args={"target": {"pattern": 1, "channel": 0}, "notes": []})


# -- send -----------------------------------------------------------------------


def test_send_writes_the_command_file_and_queues_it(tmp_path):
    carrier, root = make_carrier(tmp_path)
    carrier.send(CMD)

    on_disk = read_json(root / "commands" / "000001.json")
    assert on_disk["op"] == "add_notes"
    assert on_disk["proto"] == 2
    assert on_disk["args"] == CMD.args
    assert queue_ids(root) == [1]


def test_send_without_any_root_raises(tmp_path, monkeypatch):
    # An empty machine: no FL settings tree anywhere under HOME.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(SpoolUnavailable, match="settings folder"):
        SpoolCarrier().send(CMD)


def test_spool_root_environment_override_is_the_only_session_root(tmp_path, monkeypatch):
    root = tmp_path / "session-a"
    monkeypatch.setenv("FLMCP_SPOOL_ROOT", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-fl-settings"))

    assert spool_roots() == [root]


def test_is_live_always_the_base_medium(tmp_path):
    # The mailbox is the fallback every other carrier degrades to; whether a
    # root accepts writes is only decidable at send, where failure carries the
    # actionable message.
    carrier, _root = make_carrier(tmp_path)
    assert carrier.is_live() is True


# -- await_response -------------------------------------------------------------


def test_await_returns_consumer_response_and_prunes_queue(tmp_path):
    carrier, root = make_carrier(tmp_path)
    carrier.send(CMD)
    write_json_atomic(
        root / "responses" / f"{CMD.id:06d}.json",
        {"id": CMD.id, "ok": True, "op": "add_notes", "notes_written": 1},
    )

    resp = carrier.await_response(CMD.id, timeout=1.0)
    assert resp.ok is True
    assert resp.notes_written == 1
    assert queue_ids(root) == []


def test_await_wakes_while_polling(tmp_path):
    carrier, root = make_carrier(tmp_path)
    cmd = SpoolCommand(id=1, op="ping", args={})
    carrier.send(cmd)

    def respond_later() -> None:
        write_json_atomic(
            root / "responses" / f"{cmd.id:06d}.json",
            {"id": cmd.id, "ok": True, "op": "ping"},
        )

    timer = threading.Timer(0.15, respond_later)
    timer.start()
    try:
        assert carrier.await_response(cmd.id, timeout=2.0).ok is True
    finally:
        timer.cancel()


def test_timeout_cancels_the_command_then_raises(tmp_path):
    carrier, root = make_carrier(tmp_path)
    carrier.send(CMD)

    with pytest.raises(ToolError) as excinfo:
        carrier.await_response(CMD.id, timeout=0.05)

    assert excinfo.value.category == "note_plane_unavailable"
    assert excinfo.value.retryable is True
    # A late manual trigger must find a tombstone, not the command: the tool
    # already reported this write as failed.
    on_disk = read_json(root / "commands" / f"{CMD.id:06d}.json")
    assert on_disk == {"consumed": True, "id": CMD.id}
    assert queue_ids(root) == []


# -- helpers --------------------------------------------------------------------


def test_resume_next_id_covers_files_queue_and_ledger(tmp_path):
    root = tmp_path / "spoolroot"
    carrier = SpoolCarrier(root)
    carrier.send(SpoolCommand(id=2, op="ping", args={}))
    assert resume_next_id([root]) == 3

    # The ledger alone must also advance the counter: the consumer refuses ids
    # it has already applied, even when the server's own files are gone.
    write_json_atomic(root / "consumed.json", {"ids": [41]})
    assert resume_next_id([root]) == 42


def test_read_json_missing_is_none_silently(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        assert read_json(tmp_path / "nope.json") is None
    assert not caplog.records


def test_read_json_corruption_is_logged_never_empty(tmp_path, caplog):
    torn = tmp_path / "consumed.json"
    torn.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert read_json(torn) is None
    assert any("refusing" in r.getMessage() for r in caplog.records)


def test_write_json_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "deep" / "cmd.json"
    write_json_atomic(target, {"id": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"id": 1}
    assert [p.name for p in target.parent.iterdir()] == ["cmd.json"]
