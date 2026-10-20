"""observer: push mapping from a fake bridge_events.jsonl, the lazy
poll-and-diff checkpoint against a faked project parse, and the concurrency
guard - the docs/27 §5 surface, tested in isolation (no FL, no bridge).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest
from flpkit import FlpNote, FlpProject

from fl_mcp import errors
from fl_mcp.observer import (
    BRIDGE_EVENTS_FILE,
    GUARD_WINDOW_SECONDS,
    Observer,
    UserAction,
    UserEditing,
)
from fl_mcp.schemas import Note, NoteDelta, Target

TARGET = Target(pattern=1, channel=0)


class FakeClock:
    """Injectable time so guard-window tests never sleep."""

    def __init__(self) -> None:
        self.t = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def append_lines(root: Path, *lines: str) -> None:
    path = root / BRIDGE_EVENTS_FILE
    with path.open("a", encoding="utf-8") as f:
        f.writelines(lines)


def append_events(root: Path, *records: dict) -> None:
    append_lines(root, *(json.dumps(r) + "\n" for r in records))


# --- UserAction: the frozen event type ---------------------------------------


def test_user_action_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown UserAction kind"):
        UserAction(kind="teleport", at=datetime.now(timezone.utc), detail={})  # type: ignore[arg-type]


def test_score_edit_detail_is_validated_at_construction() -> None:
    at = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="pattern"):
        UserAction(kind="score_edit", at=at, detail={"channel": 0, "delta": NoteDelta()})
    with pytest.raises(ValueError, match="channel"):
        UserAction(kind="score_edit", at=at, detail={"pattern": 1, "delta": NoteDelta()})
    with pytest.raises(ValueError, match="NoteDelta"):
        UserAction(kind="score_edit", at=at, detail={"pattern": 1, "channel": 0, "delta": "x"})


def test_detail_is_privately_copied() -> None:
    detail = {"index": 1}
    action = UserAction(kind="focus", at=datetime.now(timezone.utc), detail=detail)
    detail["index"] = 99
    assert action.detail["index"] == 1


# --- drain_pushes: bridge_events.jsonl -> UserAction --------------------------


def test_drain_maps_each_push_callback_to_its_kind(tmp_path: Path) -> None:
    observer = Observer(roots=[tmp_path])
    append_events(
        tmp_path,
        {"seq": 1, "id": 12, "index": 0, "value": 1},  # FPD_SetPlaying
        {"seq": 2, "id": 13, "index": 0, "value": 768},  # FPD_SongPosChanged
        {"seq": 3, "id": 22, "index": 3, "value": 1},  # FPD_SetFocus
        {"seq": 4, "id": 41, "index": 5, "value": 2},  # FPD_ContextInfoChanged
        {"seq": 5, "id": 25, "index": 0, "value": 0},  # FPD_RoutingChanged
    )
    actions = observer.drain_pushes()
    assert [a.kind for a in actions] == ["transport", "position", "focus", "context", "routing"]
    assert actions[0].detail == {"index": 0, "value": 1, "seq": 1, "callback": "FPD_SetPlaying"}
    assert all(a.at.tzinfo is not None for a in actions)


def test_drain_maps_the_pinned_l4_line_shape(tmp_path: Path) -> None:
    # The full pinned schema (L4 ack 2026-08-30): raw FPD id + name + both
    # clocks + gui + cumulative dropped. `at` is the event's OWN wall clock
    # (drains are lazy), and a name-only line still maps (names outlive ids).
    observer = Observer(roots=[tmp_path])
    append_events(
        tmp_path,
        {
            "seq": 1,
            "ts_ns": 110650236040000,
            "unix_ms": 1788120000000,
            "id": 12,
            "name": "FPD_SetPlaying",
            "index": 0,
            "value": 1,
            "gui": 1,
            "dropped": 0,
        },
        {"ts_ns": 2, "name": "FPD_RoutingChanged", "index": 0, "value": 0},
    )
    actions = observer.drain_pushes()
    assert [a.kind for a in actions] == ["transport", "routing"]
    assert actions[0].at == datetime.fromtimestamp(1788120000.0, tz=timezone.utc)
    assert actions[1].detail["callback"] == "FPD_RoutingChanged"


def test_drain_surfaces_ring_drops_once_per_increase(tmp_path: Path) -> None:
    observer = Observer(roots=[tmp_path])
    append_events(
        tmp_path,
        {"seq": 1, "id": 12, "value": 1, "dropped": 0},
        {"seq": 2, "id": 12, "value": 0, "dropped": 3},
        {"seq": 3, "id": 25, "value": 0, "dropped": 3},
    )
    actions = observer.drain_pushes()
    assert "dropped_events" not in actions[0].detail
    assert actions[1].detail["dropped_events"] == 3  # stamped on the revealer
    assert "dropped_events" not in actions[2].detail  # cumulative, not repeated


def test_drain_is_incremental_across_calls(tmp_path: Path) -> None:
    observer = Observer(roots=[tmp_path])
    append_events(tmp_path, {"seq": 1, "id": 12, "value": 1})
    assert len(observer.drain_pushes()) == 1
    assert observer.drain_pushes() == []
    append_events(tmp_path, {"seq": 2, "id": 12, "value": 0})
    follow_up = observer.drain_pushes()
    assert len(follow_up) == 1 and follow_up[0].detail["seq"] == 2


def test_drain_skips_corrupt_unknown_and_blank_lines(tmp_path: Path) -> None:
    observer = Observer(roots=[tmp_path])
    append_lines(tmp_path, "{not json\n", "\n")
    append_events(
        tmp_path,
        {"seq": 1, "id": 4, "value": 44100},  # FPD_SetSampleRate: not a user action
        {"seq": 2, "id": 12, "value": 1},
    )
    actions = observer.drain_pushes()
    assert [a.kind for a in actions] == ["transport"]


def test_drain_holds_back_a_torn_tail_line(tmp_path: Path) -> None:
    observer = Observer(roots=[tmp_path])
    whole = json.dumps({"seq": 1, "id": 12, "value": 1}) + "\n"
    torn = json.dumps({"seq": 2, "id": 12, "value": 0})
    append_lines(tmp_path, whole, torn[:10])  # a mid-append read
    assert [a.detail["seq"] for a in observer.drain_pushes()] == [1]
    append_lines(tmp_path, torn[10:] + "\n")
    assert [a.detail["seq"] for a in observer.drain_pushes()] == [2]


def test_drain_restarts_from_zero_when_the_file_shrinks(tmp_path: Path) -> None:
    observer = Observer(roots=[tmp_path])
    append_events(tmp_path, {"seq": 100, "id": 12, "value": 1}, {"seq": 101, "id": 12, "value": 0})
    assert len(observer.drain_pushes()) == 2
    (tmp_path / BRIDGE_EVENTS_FILE).write_text("")  # the bridge restarted
    append_events(tmp_path, {"seq": 1, "id": 25, "value": 0})
    actions = observer.drain_pushes()
    assert [a.kind for a in actions] == ["routing"]


def test_drain_dedupes_the_event_mirrored_to_both_roots(tmp_path: Path) -> None:
    # The bridge writes every event to both spool roots (pinned schema);
    # identity is the exact line, so the second sighting is dropped.
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
    observer = Observer(roots=roots)
    for root in roots:
        append_events(root, {"seq": 7, "ts_ns": 500, "id": 12, "value": 1})
    append_events(roots[1], {"seq": 8, "ts_ns": 600, "id": 13, "value": 96})
    actions = observer.drain_pushes()
    assert [a.detail["seq"] for a in actions] == [7, 8]


def test_drain_survives_a_seq_restart_without_truncation(tmp_path: Path) -> None:
    # A plugin reload restarts seq at 1 but only truncates past ~4MB (pinned
    # schema): the new session's lines APPEND after the old ones and must
    # flow - a seq-keyed dedupe would eat the whole new session.
    observer = Observer(roots=[tmp_path])
    append_events(tmp_path, {"seq": 1, "ts_ns": 100, "id": 12, "value": 1})
    assert len(observer.drain_pushes()) == 1
    append_events(tmp_path, {"seq": 1, "ts_ns": 999, "id": 12, "value": 0})  # new session
    actions = observer.drain_pushes()
    assert [a.detail["ts_ns"] for a in actions] == [999]


def test_history_before_attach_is_state_not_news(tmp_path: Path) -> None:
    append_events(tmp_path, {"seq": 1, "id": 12, "value": 1}, {"seq": 2, "id": 12, "value": 0})
    observer = Observer(roots=[tmp_path])  # attaches at the file's end
    assert observer.drain_pushes() == []
    append_events(tmp_path, {"seq": 3, "id": 25, "value": 0})
    assert [a.kind for a in observer.drain_pushes()] == ["routing"]


def test_drain_with_no_events_file_is_quiet(tmp_path: Path) -> None:
    assert Observer(roots=[tmp_path]).drain_pushes() == []


# --- poll: lazy poll-and-diff for score edits ---------------------------------


def flp_note(
    key: int, *, pattern: int = 1, channel: int = 0, position: int = 0, velocity: int = 100
) -> FlpNote:
    return FlpNote(
        position=position, length=96, key=key, channel=channel, velocity=velocity, pan=64, pattern=pattern
    )


@pytest.fixture
def score_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A faked project-file parse: tests mutate ``notes`` and bump the mtime;
    ``reads`` counts parses so laziness is assertable."""
    project = tmp_path / "agent.flp"
    project.write_bytes(b"stub")
    state = {"notes": [], "reads": 0}
    mtimes = count(1_000_000_000, 1_000_000_000)

    def fake_read(path: Path) -> FlpProject:
        state["reads"] += 1
        return FlpProject(ppq=96, tempo=120.0, channels=[], notes=list(state["notes"]), playlist=[])

    monkeypatch.setattr("fl_mcp.flp.read", fake_read)
    clock = FakeClock()
    observer = Observer(project, roots=[tmp_path], clock=clock)

    def save(notes: list[FlpNote]) -> None:
        state["notes"] = notes
        ns = next(mtimes)
        os.utime(project, ns=(ns, ns))

    save([])  # the project exists with a stable mtime before anyone polls
    return observer, save, state, clock


def test_first_poll_seeds_silently_then_diffs_user_saves(score_env) -> None:
    observer, save, state, _clock = score_env
    save([flp_note(60)])
    assert observer.poll() == []  # pre-existing notes are state, not actions
    save([flp_note(60), flp_note(64, position=96)])
    actions = observer.poll()
    assert len(actions) == 1
    action = actions[0]
    assert action.kind == "score_edit"
    assert action.detail["pattern"] == 1 and action.detail["channel"] == 0
    delta = action.detail["delta"]
    assert [n.key for n in delta.added] == [64] and not delta.removed and not delta.moved
    assert delta.added[0] == Note(key=64, start=1.0, length=1.0, velocity=100 / 127, pan=0.5)


def test_poll_clamps_fl_raw_velocity_128(score_env) -> None:
    """FL's 128 byte value must not make a project checkpoint unreadable."""
    observer, save, _state, _clock = score_env
    save([])
    observer.poll()
    save([flp_note(60, velocity=128)])

    assert observer.poll()[0].detail["delta"].added[0].velocity == 1.0


def test_poll_reports_each_touched_target_separately(score_env) -> None:
    observer, save, _state, _clock = score_env
    save([flp_note(60)])
    observer.poll()
    save([flp_note(62), flp_note(70, pattern=2, channel=3)])
    actions = observer.poll()
    assert [(a.detail["pattern"], a.detail["channel"]) for a in actions] == [(1, 0), (2, 3)]
    # A nearby pitch change reads as a drag, not delete+insert (store pairing).
    moves = actions[0].detail["delta"].moved
    assert [(m.from_.key, m.to.key) for m in moves] == [(60, 62)]


def test_poll_is_lazy_no_reparse_while_mtime_stands(score_env) -> None:
    observer, save, state, _clock = score_env
    save([flp_note(60)])
    observer.poll()
    parses = state["reads"]
    assert observer.poll() == [] and observer.poll() == []
    assert state["reads"] == parses  # stat-only while the file is unchanged


def test_poll_without_a_project_is_empty() -> None:
    assert Observer(None, roots=[]).poll() == []


def test_unparseable_save_is_skipped_not_diffed(score_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from fl_mcp import flp

    observer, save, _state, _clock = score_env
    save([flp_note(60)])
    observer.poll()

    def explode(path: Path) -> FlpProject:
        raise flp.FlpError("torn write")

    monkeypatch.setattr("fl_mcp.flp.read", explode)
    save([])
    assert observer.poll() == []  # refused, resolved at the next good checkpoint


def test_guard_refuses_when_checkpoint_parse_fails(
    score_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fl_mcp import flp

    observer, save, _state, _clock = score_env
    save([flp_note(60)])
    observer.poll()

    def explode(path: Path) -> FlpProject:
        raise flp.FlpError("torn write")

    monkeypatch.setattr("fl_mcp.flp.read", explode)
    save([])

    with pytest.raises(errors.ToolError) as excinfo:
        observer.guard_write(TARGET)

    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable
    assert "someone is editing this track" in excinfo.value.message
    assert "overwrite their edit" in excinfo.value.message


def test_guard_refuses_when_project_file_disappears(score_env, caplog) -> None:
    observer, _save, _state, _clock = score_env
    assert observer._project is not None
    observer._project.unlink()

    with caplog.at_level("WARNING"), pytest.raises(errors.ToolError) as excinfo:
        observer.guard_write(TARGET)

    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable
    assert any("could not stat project file" in r.message for r in caplog.records), "the operator's detail is logged"


# --- the concurrency guard ----------------------------------------------------


def test_guard_refuses_the_freshly_edited_target(score_env) -> None:
    observer, save, _state, _clock = score_env
    save([])
    observer.poll()
    save([flp_note(60)])
    with pytest.raises(UserEditing) as excinfo:
        observer.guard_write(TARGET)  # guard runs the checkpoint itself
    assert isinstance(excinfo.value, errors.ToolError)
    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable
    assert excinfo.value.action.kind == "score_edit"
    # An untouched target is not gated.
    observer.guard_write(Target(pattern=9, channel=9))


def test_guard_clears_after_the_window(score_env) -> None:
    observer, save, _state, clock = score_env
    save([])
    observer.poll()
    save([flp_note(60)])
    with pytest.raises(UserEditing):
        observer.guard_write(TARGET)
    clock.advance(GUARD_WINDOW_SECONDS + 0.1)
    observer.guard_write(TARGET)  # the user paused; the write may proceed


def test_recorded_agent_write_neither_events_nor_self_blocks(score_env) -> None:
    observer, save, _state, _clock = score_env
    save([])
    observer.poll()
    agent_notes = [Note(key=60, start=0.0, length=1.0, velocity=100 / 127, pan=0.5)]
    # The lane writes, verifies by readback, then reports what landed.
    save([flp_note(60)])
    observer.record_agent_write(TARGET, agent_notes)
    assert observer.poll() == []  # the agent's own write is not a user action
    observer.guard_write(TARGET)  # and never gates the agent against itself


def test_record_agent_write_cannot_fail_an_applied_write(score_env, monkeypatch, caplog) -> None:
    """Observer bookkeeping is advisory after a confirmed write, never a failure path."""
    observer, save, _state, _clock = score_env
    save([])
    observer.poll()

    class BrokenBaseline(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("stale baseline")

    observer._baseline = BrokenBaseline()
    with caplog.at_level("ERROR"):
        assert observer.record_agent_write(TARGET, [Note(key=60, start=0, length=1)]) is False
    assert any("could not record confirmed agent write" in record.message for record in caplog.records)


def test_user_edit_beside_an_agent_write_still_surfaces(score_env) -> None:
    observer, save, _state, _clock = score_env
    save([])
    observer.poll()
    # One save carries both an agent write (channel 0, reported) and a user
    # edit (channel 1, not reported): only the user's half becomes an event.
    save([flp_note(60), flp_note(72, channel=1)])
    observer.record_agent_write(TARGET, [Note(key=60, start=0.0, length=1.0, velocity=100 / 127, pan=0.5)])
    actions = observer.poll()
    assert [(a.detail["pattern"], a.detail["channel"]) for a in actions] == [(1, 1)]
    with pytest.raises(UserEditing):
        observer.guard_write(Target(pattern=1, channel=1))
    observer.guard_write(TARGET)
