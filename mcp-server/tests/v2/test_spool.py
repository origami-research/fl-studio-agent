"""v2 spool: the ONE sequenced protocol, tested against a tmp_path root.

The consumer side (the piano-roll pyscript) is exercised separately in
test_pyscript.py under the FL sandbox emulation; here we prove the server
side: validated + normalized commands, race-free ids, atomic files, honest
timeouts, and no id reuse across restarts.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from fl_mcp.errors import ToolError
from fl_mcp.v2.spool import Spool, SpoolUnavailable, read_json, write_json_atomic


def make_spool(tmp_path) -> tuple[Spool, Path]:
    root = tmp_path / "spoolroot"
    return Spool(root=root), root


def queue_ids(root) -> list[int]:
    payload = read_json(root / "commands" / "queue.json")
    return (payload or {}).get("ids", [])


NOTE = {"key": "C4", "start": 0, "length": 1}
TARGET = {"pattern": 1, "channel": 0}


# -- send -----------------------------------------------------------------------


def test_send_writes_sequenced_validated_command(tmp_path):
    s, root = make_spool(tmp_path)
    cmd = s.send("add_notes", target=TARGET, notes=[NOTE], mode="merge")

    assert cmd.id == 1
    on_disk = read_json(root / "commands" / "000001.json")
    assert on_disk["op"] == "add_notes"
    assert on_disk["proto"] == 2
    # Unions resolve at the boundary: the consumer must never see "C4".
    assert on_disk["args"]["notes"][0]["key"] == 60
    assert on_disk["args"]["notes"][0]["velocity"] == 0.8
    assert queue_ids(root) == [1]


def test_send_rejects_bad_args_and_unknown_ops(tmp_path):
    s, root = make_spool(tmp_path)
    with pytest.raises(ValidationError):
        s.send("add_notes", target=TARGET, notes=[])  # min_length=1
    with pytest.raises(ValueError, match="unknown spool op"):
        s.send("evil_op")
    # Nothing half-written: a rejected command leaves no file and no queue entry.
    assert queue_ids(root) == []
    assert not list((root / "commands").glob("0*.json"))


def test_ids_are_sequential_and_race_free(tmp_path):
    s, root = make_spool(tmp_path)
    ids: list[int] = []

    def send_one() -> None:
        ids.append(s.send("ping").id)

    threads = [threading.Thread(target=send_one) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(ids) == list(range(1, 17)), "concurrent sends must never share an id"
    assert queue_ids(root) == list(range(1, 17))


def test_restart_never_reuses_ids(tmp_path):
    root = tmp_path / "spoolroot"
    first = Spool(root=root)
    first.send("ping")
    first.send("ping")

    assert Spool(root=root).send("ping").id == 3

    # The ledger alone must also advance the counter: the consumer refuses ids
    # it has already applied, even when the server's own files are gone.
    write_json_atomic(root / "consumed.json", {"ids": [41]})
    fresh_root_spool = Spool(root=root)
    assert fresh_root_spool.send("ping").id == 42


def test_send_without_any_root_raises(tmp_path, monkeypatch):
    # An empty machine: no FL settings tree anywhere under HOME.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    s = Spool()
    with pytest.raises(SpoolUnavailable, match="settings folder"):
        s.send("ping")


# -- await_response -------------------------------------------------------------


def test_await_returns_consumer_response_and_prunes_queue(tmp_path):
    s, root = make_spool(tmp_path)
    cmd = s.send("add_notes", target=TARGET, notes=[NOTE])
    write_json_atomic(
        root / "responses" / f"{cmd.id:06d}.json",
        {"id": cmd.id, "ok": True, "op": "add_notes", "notes_written": 1},
    )

    resp = s.await_response(cmd, timeout=1.0)
    assert resp.ok is True
    assert resp.notes_written == 1
    assert queue_ids(root) == []


def test_await_wakes_while_polling(tmp_path):
    s, root = make_spool(tmp_path)
    cmd = s.send("ping")

    def respond_later() -> None:
        write_json_atomic(
            root / "responses" / f"{cmd.id:06d}.json",
            {"id": cmd.id, "ok": True, "op": "ping"},
        )

    timer = threading.Timer(0.15, respond_later)
    timer.start()
    try:
        assert s.await_response(cmd, timeout=2.0).ok is True
    finally:
        timer.cancel()


def test_timeout_cancels_the_command_then_raises(tmp_path):
    s, root = make_spool(tmp_path)
    cmd = s.send("add_notes", target=TARGET, notes=[NOTE])

    with pytest.raises(ToolError) as excinfo:
        s.await_response(cmd, timeout=0.05)

    assert excinfo.value.category == "note_plane_unavailable"
    assert excinfo.value.retryable is True
    # A late manual trigger must find a tombstone, not the command: the tool
    # already reported this write as failed.
    on_disk = read_json(root / "commands" / f"{cmd.id:06d}.json")
    assert on_disk == {"consumed": True, "id": cmd.id}
    assert queue_ids(root) == []


# -- helpers --------------------------------------------------------------------


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
