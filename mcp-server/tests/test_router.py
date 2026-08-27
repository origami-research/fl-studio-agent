"""Tests for the backend router's write-through (docs/17): whichever backend
applies, the shadow score and action log record the same structured delta."""

from fl_mcp import router, schemas
from fl_mcp.actions import ActionLog
from fl_mcp.records import ShadowStore


def _stores(tmp_path):
    return ShadowStore("test", root=tmp_path / "shadow"), ActionLog("test", root=tmp_path / "actions")


def test_record_note_write_updates_shadow_and_logs_delta(tmp_path):
    shadow, actions = _stores(tmp_path)
    target = schemas.Target(pattern=1, channel=2)
    notes = [{"key": "C4", "start": 0, "length": 1}]

    delta = router.record_note_write(shadow, actions, target, notes, "replace", "project-write", command_id=7)

    # Shadow score now holds the notes.
    assert len(shadow.notes_at(target)) == 1
    # A delta was returned and logged.
    assert delta is not None and len(delta.added) == 1 and delta.added[0].key == "C4"
    last = actions.last()
    assert last.op == "add_notes" and last.after_hash is not None
    assert last.delta is not None and len(last.delta.added) == 1
    assert last.undo_step.startswith("project-write:")


def test_record_note_write_merge_delta_is_only_the_addition(tmp_path):
    shadow, actions = _stores(tmp_path)
    target = schemas.Target(pattern=1, channel=3)
    router.record_note_write(shadow, actions, target, [{"key": "C4", "start": 0, "length": 1}], "replace", "live", 1)
    router.record_note_write(shadow, actions, target, [{"key": "G4", "start": 2, "length": 1}], "merge", "live", 2)

    last = actions.last()
    assert [n.key for n in last.delta.added] == ["G4"]
    assert not last.delta.removed
    assert len(shadow.notes_at(target)) == 2


def test_record_note_write_never_raises_on_bad_input(tmp_path):
    shadow, actions = _stores(tmp_path)
    # A malformed note would raise inside; the recorder must swallow and return None.
    delta = router.record_note_write(
        shadow, actions, schemas.Target(), [{"key": "H9", "start": 0, "length": 1}], "merge", "live", 1
    )
    assert delta is None


def test_route_note_write_tries_in_order_and_reports(tmp_path):
    calls = []

    def backend_a(parsed, mode, channel, cid):
        calls.append("a")
        return None  # falls through

    def backend_b(parsed, mode, channel, cid):
        calls.append("b")
        return {"status": "applied", "backend": "b"}

    applied = []
    result = router.route_note_write(
        [("a", backend_a), ("b", backend_b)],
        [{"key": "C4", "start": 0, "length": 1}],
        "merge",
        None,
        command_id=1,
        on_applied=lambda name, res: applied.append(name),
    )
    assert calls == ["a", "b"]  # tried a, then b
    assert result["backend"] == "b"
    assert applied == ["b"]  # on_applied fired for the backend that applied


# --- live control cascade (set_tempo prefers the live plane) ---------------

def test_set_tempo_uses_live_control_plane_when_connected(tmp_path):
    from fake_fl import FakeControl

    from fl_mcp import server
    from fl_mcp.session import FLSession

    control = FakeControl()
    control.send_hello()
    session = FLSession(project_key="t", spool_root=tmp_path / "s", project_root=tmp_path / "p", control=control)
    server.set_session(session)
    try:
        result = server.set_tempo(140)
        assert result["status"] == "applied"
        assert result["backend"] == "live"
        assert result["tempo"] == 140
        assert control.tempo == 140  # actually changed in the fake session
    finally:
        server.set_session(None)


def test_set_tempo_rejects_out_of_range(tmp_path):
    from fl_mcp import server

    server.set_session(None)
    assert server.set_tempo(5)["status"] == "error"
    assert server.set_tempo(2000)["status"] == "error"


# --- undo_last + set_channel_mix over the live control plane ----------------

def _live_session(tmp_path):
    from fake_fl import FakeControl
    from fl_mcp.session import FLSession
    control = FakeControl()
    control.send_hello()
    return FLSession(project_key="t", spool_root=tmp_path / "s", project_root=tmp_path / "p", control=control), control


def test_undo_last_invokes_control_plane_when_connected(tmp_path):
    from fl_mcp import schemas, server
    session, _ = _live_session(tmp_path)
    server.set_session(session)
    try:
        # record an action so there's something to name
        session.actions.append(schemas.ActionEntry(ts=__import__("datetime").datetime.now(), id=1, gen=0, op="add_notes", undo_step="mcp: x"))
        result = server.undo_last()
        assert result["status"] == "applied" and result["backend"] == "live"
        assert result["undone"]["op"] == "add_notes"
    finally:
        server.set_session(None)


def test_undo_last_identifies_when_no_control_plane(tmp_path):
    from fl_mcp import schemas, server
    from fl_mcp.session import FLSession
    session = FLSession(project_key="t", spool_root=tmp_path / "s", project_root=tmp_path / "p", control=None)
    session.actions.append(schemas.ActionEntry(ts=__import__("datetime").datetime.now(), id=1, gen=0, op="add_notes", undo_step="mcp: x"))
    server.set_session(session)
    try:
        result = server.undo_last()
        assert result["status"] == "identified"
    finally:
        server.set_session(None)


def test_set_channel_mix_live_sets_volume_and_pan(tmp_path):
    from fl_mcp import server
    session, control = _live_session(tmp_path)
    server.set_session(session)
    try:
        result = server.set_channel_mix(channel=0, volume=0.5, pan=-0.3)
        assert result["status"] == "applied" and result["backend"] == "live"
        assert control.channels[0].volume == 0.5
        assert control.channels[0].pan == -0.3
    finally:
        server.set_session(None)


# --- sync_from_project (ground-truth read-back) ----------------------------

def test_sync_from_project_rebuilds_shadow_with_project_provenance(tmp_path, monkeypatch):
    from fl_mcp import server, schemas
    from fl_mcp.session import FLSession

    session = FLSession(project_key="t", spool_root=tmp_path / "s", project_root=tmp_path / "p", control=None)
    server.set_session(session)

    # Stand in for the .flp reader on the real module (setattr, so it holds
    # regardless of how server.py imports it).
    from fl_mcp import autonomous
    monkeypatch.setattr(autonomous, "available", lambda: True)
    monkeypatch.setattr(autonomous, "verify", lambda pattern=1: {
        "ok": True, "note_count": 2, "notes": [
            {"midi": 60, "channel": 0, "start_beat": 0.0, "length_beats": 1.0},
            {"midi": 67, "channel": 1, "start_beat": 2.0, "length_beats": 0.5},
        ],
    })
    try:
        result = server.sync_from_project(pattern=1)
        assert result["status"] == "ok" and result["source"] == "project"
        assert result["note_count"] == 2

        # get_notes now reports ground-truth provenance for each synced target.
        ch0 = server.get_notes(pattern=1, channel=0)
        assert ch0["source"] == "project" and len(ch0["notes"]) == 1 and ch0["notes"][0]["key"] == 60
        ch1 = server.get_notes(pattern=1, channel=1)
        assert ch1["source"] == "project" and ch1["notes"][0]["key"] == 67
    finally:
        server.set_session(None)


# --- regression: get_notes key filter must handle string note keys ----------

def test_get_notes_key_filter_handles_string_keys(tmp_path):
    """Notes are stored with keys like 'C4'; filtering by an int MIDI range must
    normalize them, not compare str >= int (which raises). Regression for the
    '# type: ignore[operator]' that was masking a real bug."""
    from fl_mcp import schemas, server
    from fl_mcp.session import FLSession

    session = FLSession(project_key="t", spool_root=tmp_path / "s", project_root=tmp_path / "p", control=None)
    target = schemas.Target(pattern=1, channel=0)
    session.shadow.write(
        target,
        [
            schemas.Note(key="C4", start=0, length=1),   # 60
            schemas.Note(key="C6", start=1, length=1),   # 84
            schemas.Note(key="C2", start=2, length=1),   # 36
        ],
        mode="replace",
    )
    server.set_session(session)
    try:
        result = server.get_notes(pattern=1, channel=0, key_min=48, key_max=72)
        assert result["status"] == "ok"
        keys = {n["key"] for n in result["notes"]}
        assert keys == {"C4"}  # only 60 is within [48, 72]
    finally:
        server.set_session(None)
