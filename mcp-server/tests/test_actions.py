from __future__ import annotations

from datetime import datetime, timezone

from fl_mcp import actions, schemas


def entry(i: int, **kwargs) -> schemas.ActionEntry:
    return schemas.ActionEntry(ts=datetime.now(timezone.utc), id=i, gen=0, op="add_notes", **kwargs)


def test_log_starts_empty(tmp_path):
    log = actions.ActionLog("proj", root=tmp_path)
    assert log.read_all() == []
    assert log.last() is None


def test_append_is_oldest_first_on_disk(tmp_path):
    log = actions.ActionLog("proj", root=tmp_path)
    for i in range(3):
        log.append(entry(i))
    ids = [e.id for e in log.read_all()]
    assert ids == [0, 1, 2]


def test_read_reverse_is_newest_first(tmp_path):
    log = actions.ActionLog("proj", root=tmp_path)
    for i in range(3):
        log.append(entry(i))
    ids = [e.id for e in log.read_reverse()]
    assert ids == [2, 1, 0]


def test_last_returns_the_most_recent_entry(tmp_path):
    log = actions.ActionLog("proj", root=tmp_path)
    log.append(entry(0))
    log.append(entry(1, undo_step="mcp: second"))
    last = log.last()
    assert last.id == 1
    assert last.undo_step == "mcp: second"


def test_is_append_only_across_instances(tmp_path):
    log1 = actions.ActionLog("proj", root=tmp_path)
    log1.append(entry(0))
    log2 = actions.ActionLog("proj", root=tmp_path)
    log2.append(entry(1))
    assert [e.id for e in log1.read_all()] == [0, 1]


def test_entry_round_trips_target_and_hashes(tmp_path):
    log = actions.ActionLog("proj", root=tmp_path)
    target = schemas.Target(pattern=3, channel=4)
    log.append(entry(0, target=target, mode="replace", before_hash="a" * 8, after_hash="b" * 8))
    (loaded,) = log.read_all()
    assert loaded.target == target
    assert loaded.before_hash == "a" * 8
    assert loaded.after_hash == "b" * 8
