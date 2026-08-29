from __future__ import annotations

import pytest
from fake_fl import FakeBridge, FakeChannelState, FakeControl, FakeMixerTrack

from fl_mcp import errors, kernel, schemas
from fl_mcp.session import FLSession


@pytest.fixture
def wired(tmp_path):
    control = FakeControl(
        channels=[
            FakeChannelState(0, "Kick"),
            FakeChannelState(1, "Bass"),
            FakeChannelState(2, "Lead"),
        ],
        mixer=[FakeMixerTrack(0, "Master")],
    )
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


def _target(channel: int) -> schemas.Target:
    return schemas.Target(pattern=-1, channel=channel)


# -- bootstrap ---------------------------------------------------------------


def test_bootstrap_builds_manifest_and_logs_entry(wired):
    session, bridge, control = wired

    fl = kernel.bootstrap(session)

    assert [c.name for c in fl.manifest.channels] == ["Kick", "Bass", "Lead"]
    assert (session.shadow.root / "manifest.json").is_file()
    last = session.actions.last()
    assert last.op == "bootstrap"
    assert last.after["channels"] == 3
    assert last.after["mixer_tracks"] == 1
    assert last.after["ground_truth_notes"].startswith("pending")


def test_bootstrap_with_project_reader_marks_ground_truth_read(wired):
    session, bridge, control = wired
    seen = []

    kernel.bootstrap(session, project_reader=seen.append)

    assert seen == [session]
    assert session.actions.last().after["ground_truth_notes"] == "read"


def test_bootstrap_requires_control_plane(tmp_path):
    session = FLSession(project_key="test", spool_root=tmp_path / "spool", project_root=tmp_path / "project")
    with pytest.raises(errors.ToolError) as exc_info:
        kernel.bootstrap(session)
    assert exc_info.value.category == "control_plane_unavailable"


# -- track resolution ---------------------------------------------------------


def test_track_resolves_by_name_and_caches(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")

    assert bass.channel == 1
    assert bass.name == "Bass"
    assert fl.track("Bass") is bass


def test_track_resolves_lazily_without_explicit_bootstrap(wired):
    session, bridge, control = wired
    fl = kernel.FLKernel(session)

    bass = fl.track("Bass")

    assert bass.channel == 1
    assert fl.manifest is not None


def test_track_by_number(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)

    lead = fl.track(2)

    assert lead.channel == 2
    assert lead.name == "Lead"


def test_track_unknown_name_raises_unknown_target(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)

    with pytest.raises(errors.ToolError) as exc_info:
        fl.track("Nope")
    assert exc_info.value.category == "unknown_target"
    assert "Bass" in exc_info.value.candidates


# -- write queue / flush / cell -----------------------------------------------


def test_write_is_queued_not_applied_until_flush(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")

    bass.write([schemas.Note(key="C4", start=0, length=1)])
    assert bridge.notes_at(-1, 1) == []

    results = fl.flush()

    assert results == [{"op": "add_notes", "track": "Bass", "ok": True, "notes_written": 1, "err": None}]
    assert bridge.notes_at(-1, 1)


def test_cell_flushes_on_clean_exit(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")

    with fl.cell():
        bass.write([schemas.Note(key="C4", start=0, length=1)])

    assert bridge.notes_at(-1, 1)


def test_cell_does_not_flush_on_exception(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")

    with pytest.raises(RuntimeError), fl.cell():
        bass.write([schemas.Note(key="C4", start=0, length=1)])
        raise RuntimeError("boom")

    assert bridge.notes_at(-1, 1) == []
    assert len(fl._queue) == 1


def test_clear_queues_and_flushes(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")
    bass.write([schemas.Note(key="C4", start=0, length=1)])
    fl.flush()
    assert bridge.notes_at(-1, 1)

    bass.clear()
    results = fl.flush()

    assert results == [{"op": "clear_notes", "track": "Bass", "ok": True, "err": None}]
    assert bridge.notes_at(-1, 1) == []


# -- notes() / NoteSetHandle ---------------------------------------------------


def test_notes_returns_summary_and_materializes(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")
    bass.write([schemas.Note(key="C4", start=0, length=1), schemas.Note(key="E4", start=1, length=1)])
    fl.flush()

    handle = bass.notes()

    assert len(handle) == 2
    assert "2 notes" in handle.summary
    assert {n.key for n in handle.materialize()} == {60, 64}


def test_notes_filter_predicate(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")
    bass.write([schemas.Note(key="C4", start=0, length=1), schemas.Note(key="E4", start=1, length=1)])
    fl.flush()

    handle = bass.notes(filter=lambda n: n.key >= 64)

    assert len(handle) == 1
    assert handle.materialize()[0].key == 64


def test_notes_empty_target_returns_no_notes_summary(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")

    handle = bass.notes()

    assert len(handle) == 0
    assert handle.summary == "no notes"


# -- staleness ------------------------------------------------------------


def test_structural_event_stales_handle(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")

    control.send_event("dirty_channel", index=1)

    with pytest.raises(errors.ToolError) as exc_info:
        bass.write([schemas.Note(key="C4", start=0, length=1)])
    assert exc_info.value.category == "stale_handle"

    bass2 = fl.track("Bass")
    bass2.write([schemas.Note(key="C4", start=0, length=1)])
    fl.flush()
    assert bridge.notes_at(-1, 1)


@pytest.mark.parametrize("op,fields", [("dirty_mixer_track", {"index": 0}), ("refresh", {"flags": 1})])
def test_other_structural_events_also_stale_handles(wired, op, fields):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")

    control.send_event(op, **fields)

    with pytest.raises(errors.ToolError) as exc_info:
        bass.write([schemas.Note(key="C4", start=0, length=1)])
    assert exc_info.value.category == "stale_handle"


def test_project_load_event_marks_shadow_stale_and_clears_manifest(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")
    bass.write([schemas.Note(key="C4", start=0, length=1)])
    fl.flush()
    assert session.shadow.ref_at(_target(1)).stale is False

    control.send_event("project_load", status=0)

    assert session.shadow.ref_at(_target(1)).stale is True
    assert fl.manifest is None
    with pytest.raises(errors.ToolError) as exc_info:
        bass.write([schemas.Note(key="C4", start=0, length=1)])
    assert exc_info.value.category == "stale_handle"

    # A fresh handle re-resolves the manifest lazily and works again.
    bass2 = fl.track("Bass")
    assert bass2.channel == 1


def test_queued_write_goes_stale_between_queueing_and_flush(wired):
    """A write queued while fresh, followed by a structural event before the
    cell ends, must not silently apply against a now-invalid handle."""
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")
    bass.write([schemas.Note(key="C4", start=0, length=1)])

    control.send_event("refresh", flags=1)

    with pytest.raises(errors.ToolError) as exc_info:
        fl.flush()
    assert exc_info.value.category == "stale_handle"
    assert bridge.notes_at(-1, 1) == []


# -- undo -----------------------------------------------------------------


def test_undo_last_marks_target_stale_and_bumps_generation(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")
    bass.write([schemas.Note(key="C4", start=0, length=1)])
    fl.flush()

    entry = fl.undo_last()

    assert entry.op == "add_notes"
    assert session.shadow.ref_at(_target(1)).stale is True
    with pytest.raises(errors.ToolError) as exc_info:
        bass.write([schemas.Note(key="C4", start=0, length=1)])
    assert exc_info.value.category == "stale_handle"


def test_undo_last_raises_when_nothing_recorded(wired):
    session, bridge, control = wired
    fl = kernel.FLKernel(session)  # no bootstrap => no actions logged yet

    with pytest.raises(errors.ToolError) as exc_info:
        fl.undo_last()
    assert exc_info.value.category == "fl_api_error"


# -- transport --------------------------------------------------------------


def test_play_stop_set_tempo_round_trip(wired):
    session, bridge, control = wired
    fl = kernel.bootstrap(session)

    fl.play()
    assert control.playing is True
    fl.stop()
    assert control.playing is False
    fl.set_tempo(128)
    assert control.tempo == 128.0


def test_transport_requires_control_plane(tmp_path):
    session = FLSession(project_key="test", spool_root=tmp_path / "spool", project_root=tmp_path / "project")
    fl = kernel.FLKernel(session)

    with pytest.raises(errors.ToolError) as exc_info:
        fl.play()
    assert exc_info.value.category == "control_plane_unavailable"


def test_mutating_op_reports_operation_unsafe_as_retryable(wired):
    """docs/14: FL sometimes rejects a mutating op mid-engine-cycle with its own
    'Operation unsafe at current time' error - the kernel must surface this as
    the dedicated, retryable category, not a generic fl_api_error."""
    session, bridge, control = wired
    fl = kernel.bootstrap(session)

    def _unsafe(*_args, **_kwargs):
        return schemas.ControlResponse(id=0, ok=False, err="RuntimeError: Operation unsafe at current time")

    control.send_request = _unsafe  # type: ignore[method-assign]

    with pytest.raises(errors.ToolError) as exc_info:
        fl.set_tempo(128)
    assert exc_info.value.category == "operation_unsafe"
    assert exc_info.value.retryable is True


def test_flush_stops_on_first_rejection_leaving_the_rest_queued(wired):
    """The docstring's contract: a rejected op halts the flush so later writes
    never apply against a base state that assumed the rejected one landed."""
    session, bridge, control = wired
    fl = kernel.bootstrap(session)

    fl.track("Bass").write([schemas.Note(key="C4", start=0, length=1)])
    fl.track("Lead").write([schemas.Note(key="E4", start=0, length=1)])

    bridge.fail_ops.add("add_notes")
    results = fl.flush()

    assert len(results) == 1 and results[0]["ok"] is False  # stopped at the rejection
    assert bridge.notes_at(-1, 2) == []  # Lead write did not apply

    bridge.fail_ops.clear()
    retry = fl.flush()  # the remainder stayed queued and applies cleanly now
    assert [r["ok"] for r in retry] == [True]
    assert bridge.notes_at(-1, 2)
