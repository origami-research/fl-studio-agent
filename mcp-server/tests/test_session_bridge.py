"""The bridge lane rides capability detection + a circuit breaker - no flag.

FL_MCP_BRIDGE is DEAD (docs/27 §2, doctrine #54). Eligibility is evidence,
per call: a fresh proto-matched heartbeat, a wire-verified selected channel
(the bridge cannot see its own channel - heartbeat chan= is always -1,
VERDICT Q2), and a ping-gated breaker. Receipts never trust the bridge's
echo; test_session_notes.py stays authoritative for the file lane.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flpkit import FlpNote
from session_fakes import (
    HELLO,
    NOTES,
    TARGET,
    FakeControl,
    FakeSpool,
    make_session,
)

from fl_mcp import errors, flp
from fl_mcp.contract import FLLicense
from fl_mcp.core.router import route
from fl_mcp.ops import notes as ops_notes
from fl_mcp.schemas import Note, SpoolResponse, Target
from fl_mcp.session import FLSession, WriteRejected
from fl_mcp.session import detect as detect_mod
from fl_mcp.session import notes as notes_mod
from fl_mcp.session.detect import (
    BRIDGE_HEARTBEAT_FILE,
    REOPEN_PROBE_MIN_INTERVAL_SECONDS,
    BridgeBreaker,
    Capabilities,
    read_heartbeat,
)
from fl_mcp.session.notes import BridgeLane, FileLane
from fl_mcp.template import template_path

PING_OK = SpoolResponse(id=0, ok=True, op="ping")
BRIDGE_OK = SpoolResponse(id=0, ok=True, op="add_notes", notes_written=999)  # 999: a lying echo

LIVE_PROFILE = SimpleNamespace(capabilities=SimpleNamespace(channel_targeting_live=True))


def write_heartbeat(path: Path, *, unix_time: int | None = None, proto: int | None = 2, abi: str | None = "ok") -> None:
    """A heartbeat as L4's bridge writes it (docs/27 §6): chan=-1 always (no
    FL getter for the selected channel exists), pat=-1 until a first write."""
    lines = ["tick=12", "applied=0", "pid=1", "backend=bridge"]
    lines.append(f"unix_time={int(time.time()) if unix_time is None else unix_time}")
    if proto is not None:
        lines.append(f"proto={proto}")
    if abi is not None:
        lines.append(f"abi={abi}")
    lines += ["pat=-1", "chan=-1"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def routed(session: FLSession) -> str:
    """Which lane served the last write: the backend the session recorded."""
    return session.store.writes[-1][4]


def template_copy(tmp_path: Path) -> Path:
    """A real copy of the session template, for writes that route to the file lane."""
    path = tmp_path / "agent.flp"
    shutil.copyfile(template_path(), path)
    return path


@pytest.fixture()
def no_reload(monkeypatch):
    """Never let a unit test SIGKILL or launch FL; record the calls instead."""
    calls: list[bool] = []
    monkeypatch.setattr(FLSession, "_reload_fl", lambda self: calls.append(True))
    return calls


@pytest.fixture()
def bridge_env(tmp_path, monkeypatch):
    """Spool rooted at tmp_path, heartbeat fresh + proto-matched; tests
    degrade it."""
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [tmp_path])
    heartbeat = tmp_path / BRIDGE_HEARTBEAT_FILE
    write_heartbeat(heartbeat)
    return heartbeat


def selected_channel_control(channel: int = 0) -> FakeControl:
    """A live wire answering the R2 §3.2 guard: which channel is selected."""
    return FakeControl(hello=HELLO, handlers={"channels.selectedChannel": lambda args: channel})


def project_flp(monkeypatch, tmp_path, *, ppq=192, file_notes=2):
    """The flp seams the bridge path touches: ppq for the stamp, notes_at for
    the readback. write_notes raises - the bridge path must never file-write."""
    monkeypatch.setattr(flp, "read", lambda path: SimpleNamespace(ppq=ppq, notes=[], channels=()))
    monkeypatch.setattr(
        flp,
        "notes_at",
        lambda path, pattern, channel: (
            [FlpNote(position=0, length=96, key=60, channel=channel, velocity=100, pan=64)] * file_notes
        ),
    )

    def never(*a, **k):
        raise AssertionError("the bridge path must not write the project file")

    monkeypatch.setattr(flp, "write_notes", never)
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    return project


# -- eligibility: evidence in, lane out ---------------------------------------


def test_fresh_heartbeat_and_matching_channel_routes_bridge(monkeypatch, tmp_path, bridge_env, no_reload):
    project = project_flp(monkeypatch, tmp_path, ppq=192, file_notes=2)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, store = make_session(control=selected_channel_control(0), transport=spool, project=project)

    result = session.write_notes(TARGET, NOTES, "merge")

    assert routed(session) == "bridge"
    # Ping-gated first use, then the spooled command carries the project's
    # ppq; the consumer never guesses.
    assert spool.sent == [
        ("ping", {}),
        ("add_notes", {"target": TARGET, "notes": NOTES, "mode": "merge", "ppq": 192}),
    ]
    # A licensed FL saved, so the total is the saved file's readback, NEVER the echo (999).
    assert result.total == 2
    # Recorded against the spool command id, and FL was never reloaded (the
    # write landed live; a reload would destroy it).
    assert store.writes[0][3:] == ("merge", "bridge", 2)
    assert no_reload == []


def test_ping_gates_only_the_first_use(monkeypatch, tmp_path, bridge_env, no_reload):
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(control=selected_channel_control(0), transport=spool, project=project)

    session.write_notes(TARGET, NOTES, "merge")
    session.write_notes(TARGET, NOTES, "merge")

    assert [op for op, _ in spool.sent] == ["ping", "add_notes", "add_notes"]


def test_bridge_clear_spools_clear_notes(monkeypatch, tmp_path, bridge_env, no_reload):
    project = project_flp(monkeypatch, tmp_path, file_notes=0)
    spool = FakeSpool(
        responses={
            "ping": PING_OK,
            "clear_notes": SpoolResponse(id=0, ok=True, op="clear_notes", notes_written=0),
        }
    )
    session, store = make_session(control=selected_channel_control(0), transport=spool, project=project)

    result = session.clear_notes(TARGET)

    assert result.total == 0
    assert spool.sent == [("ping", {}), ("clear_notes", {"target": TARGET})]
    assert store.writes[0][2:] == ([], "replace", "bridge", 2)
    assert no_reload == []


def test_stale_heartbeat_routes_project_write(monkeypatch, tmp_path, bridge_env, no_reload):
    write_heartbeat(bridge_env, unix_time=int(time.time() - 3600))
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=template_copy(tmp_path)
    )

    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "project-write"
    assert spool.sent == [], "a dead bridge must never receive commands"


def test_stale_heartbeat_refuses_a_session_write_before_the_bridge_timeout(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """Session mode has no safe file fallback, so a stopped tick fails immediately."""
    write_heartbeat(bridge_env, unix_time=int(time.time() - 3600))
    # The bridge port file names a LIVE FL: the tick stopped while FL is up.
    (tmp_path / "bridge_udp.json").write_text(
        json.dumps({"proto": 2, "transport": 1, "port": 1, "token": "t", "pid": os.getpid()})
    )
    writes: list[bool] = []
    monkeypatch.setattr(flp, "write_notes", lambda *args, **kwargs: writes.append(True))
    spool = FakeSpool()
    session, _ = make_session(
        control=selected_channel_control(0),
        transport=spool,
        project=template_copy(tmp_path),
        session_spool_root=tmp_path,
    )

    with pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")

    # The producer hears the diagnosis (a loaded bridge whose GUI-thread tick
    # stopped: locked Mac, modal dialog), never "requires reopening the file".
    assert excinfo.value.category == "note_plane_unavailable"
    assert "stopped responding" in excinfo.value.message
    assert "locked screen" in excinfo.value.fix
    assert excinfo.value.retryable is True
    assert spool.sent == []
    assert writes == []


def test_stale_heartbeat_with_fl_gone_is_not_blamed_on_the_gui_thread(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """No live bridge pid: the stopped tick is FL being gone, so the session
    file-lane refusal stands and the liveness path owns the story."""
    write_heartbeat(bridge_env, unix_time=int(time.time() - 3600))
    monkeypatch.setattr("fl_mcp.session.notes.pid_alive", lambda pid: False)
    monkeypatch.setattr(flp, "write_notes", lambda *args, **kwargs: None)
    session, _ = make_session(
        control=selected_channel_control(0),
        transport=FakeSpool(),
        project=template_copy(tmp_path),
        session_spool_root=tmp_path,
    )
    with pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")
    assert excinfo.value.category == "file_lane_requires_reload"


@pytest.mark.parametrize("proto", [None, 1], ids=["missing", "mismatched"])
def test_unspoken_heartbeat_proto_routes_project_write(monkeypatch, tmp_path, bridge_env, no_reload, proto):
    """A heartbeat whose protocol the server does not speak is refused -
    exactly analogous to the wire's dropped-frames codec check."""
    write_heartbeat(bridge_env, proto=proto)
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=template_copy(tmp_path)
    )

    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "project-write"
    assert spool.sent == []


def test_matching_target_channel_skips_selection(monkeypatch, tmp_path, bridge_env, no_reload):
    """The selected target needs no control mutation before its Bridge write."""
    project = project_flp(monkeypatch, tmp_path)
    control = selected_channel_control(0)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(control=control, transport=spool, project=project)

    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "bridge"
    assert "select_channels" not in [op for op, _ in control.calls]


def test_mismatched_target_selects_and_verifies_before_bridge_write(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """Q2 regression: select exactly the target, verify it, then write live."""
    project = project_flp(monkeypatch, tmp_path)
    selected = {"channel": 3}

    def select(args):
        assert args == [[0]]
        selected["channel"] = args[0][0]

    control = FakeControl(
        hello=HELLO,
        handlers={
            "channels.selectedChannel": lambda args: selected["channel"],
            "select_channels": select,
        },
    )
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(control=control, transport=spool, project=project)

    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "bridge"
    assert [op for op, _ in control.calls].count("select_channels") == 1
    assert session.caps.bridge_channel == 0
    assert [op for op, _ in spool.sent] == ["ping", "add_notes"]


def test_channel_selection_readback_mismatch_refuses_before_bridge_command(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """A failed selection readback is retryable and can never write the wrong roll."""
    project = project_flp(monkeypatch, tmp_path)
    control = FakeControl(
        hello=HELLO,
        handlers={
            "channels.selectedChannel": lambda args: 3,
            "select_channels": lambda args: None,
        },
    )
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(control=control, transport=spool, project=project)

    with pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")

    assert excinfo.value.category == "fl_api_error"
    assert excinfo.value.retryable is True
    assert "channel 3" in excinfo.value.message
    assert spool.sent == [("ping", {})]


def test_wire_down_cannot_prove_the_channel_routes_project_write(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """R2 §3.2: the bridge cannot see its own channel (heartbeat chan=-1) and
    with the wire down nobody can - correctness beats liveness."""
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool(responses={"ping": PING_OK})
    session, _ = make_session(control=None, transport=spool, project=template_copy(tmp_path))

    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "project-write"
    assert spool.sent == []


def test_profile_live_channel_targeting_widens_eligibility(monkeypatch, tmp_path, bridge_env, no_reload):
    """The live profile admits a mismatch only when the guard can select it."""
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    selected = {"channel": 3}
    session, _ = make_session(
        control=FakeControl(
            hello=HELLO,
            handlers={
                "channels.selectedChannel": lambda args: selected["channel"],
                "select_channels": lambda args: selected.update(channel=args[0][0]),
            },
        ),
        transport=spool,
        project=project,
        profile=LIVE_PROFILE,
    )

    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "bridge"


# -- the breaker at the session seam ------------------------------------------


def test_prewarm_closes_the_breaker_before_the_first_bridge_write(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(control=selected_channel_control(0), transport=spool, project=project)

    session.prewarm()

    assert session.breaker.closed is True
    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "bridge"
    assert [op for op, _ in spool.sent] == ["ping", "add_notes"]


def test_failed_prewarm_keeps_the_first_write_on_the_file_lane(monkeypatch, tmp_path, bridge_env, no_reload):
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()  # no ping response: the bridge cannot prove its pumps run
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=template_copy(tmp_path)
    )

    session.prewarm()

    assert session.breaker.closed is False
    session.write_notes(TARGET, NOTES, "merge")
    assert routed(session) == "project-write"
    assert [op for op, _ in spool.sent] == ["ping", "ping"]


def test_failed_ping_falls_back_to_file_for_that_call(monkeypatch, tmp_path, bridge_env, no_reload):
    """R4 B2-7's failure mode: a wedged GUI thread keeps writing heartbeats
    while pumps never run. The ping gate catches it and the call lands on the
    file lane instead of burning the command timeout."""
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()  # no scripted ping response: the round trip times out
    session, store = make_session(
        control=selected_channel_control(0), transport=spool, project=template_copy(tmp_path)
    )

    session.write_notes(TARGET, NOTES, "merge")

    assert [op for op, _ in spool.sent] == ["ping"]
    assert store.writes[0][3:] == ("merge", "project-write", 0)
    assert session.breaker.closed is False


def test_bridge_refusal_raises_and_records_nothing(monkeypatch, tmp_path, bridge_env, no_reload, caplog):
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(
        responses={
            "ping": PING_OK,
            "add_notes": SpoolResponse(id=0, ok=False, op="add_notes", err="no plugin host"),
        }
    )
    session, store = make_session(control=selected_channel_control(0), transport=spool, project=project)

    with caplog.at_level("WARNING"), pytest.raises(WriteRejected, match="refused the note edit"):
        session.write_notes(TARGET, NOTES, "merge")
    assert any("no plugin host" in r.message for r in caplog.records), "the Bridge's reason is logged"
    assert store.writes == []
    assert no_reload == []
    assert session.breaker.closed is False, "a refusal re-arms the ping gate"


def test_bridge_timeout_propagates_and_soft_fails_while_fl_lives(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    project = project_flp(monkeypatch, tmp_path)
    monkeypatch.setattr(notes_mod, "fl_process_running", lambda: True)
    spool = FakeSpool(responses={"ping": PING_OK})  # no add_notes response
    session, store = make_session(control=selected_channel_control(0), transport=spool, project=project)

    with pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")
    assert excinfo.value.category == "note_plane_unavailable"
    assert store.writes == []
    assert session.breaker.closed is False
    assert session.breaker.last_hard_fingerprint is None


def test_bridge_timeout_with_fl_dead_is_a_hard_failure(monkeypatch, tmp_path, bridge_env, no_reload):
    """An unacked command with FL gone: the command may have been the trigger,
    so the breaker opens outright and remembers the fingerprint."""
    project = project_flp(monkeypatch, tmp_path)
    monkeypatch.setattr(notes_mod, "fl_process_running", lambda: False)
    spool = FakeSpool(responses={"ping": PING_OK})
    session, _ = make_session(control=selected_channel_control(0), transport=spool, project=project)

    with pytest.raises(errors.ToolError):
        session.write_notes(TARGET, NOTES, "merge")
    assert session.breaker.closed is False
    assert "unacked" in session.breaker.last_hard_fingerprint


def test_failed_saved_readback_leaves_the_acknowledgement_as_confirmation(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """A torn project file: FL's acknowledgement still confirms a replace;
    a merge onto a baseline nobody can read has no knowable total."""
    monkeypatch.setattr(flp, "read", lambda path: SimpleNamespace(ppq=96, channels=(), notes=()))

    def torn(path, pattern, channel):
        raise flp.FlpError("torn header")

    monkeypatch.setattr(flp, "notes_at", torn)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    session, store = make_session(control=selected_channel_control(0), transport=spool, project=project)

    other = Target(pattern=2, channel=0)
    assert session.write_notes(other, NOTES, "merge").total is None
    assert store.writes == [], "an unknowable state is never recorded as truth"
    assert session.write_notes(TARGET, NOTES, "replace").total == 1
    assert session.read_notes(TARGET) == NOTES


def test_trial_bridge_write_is_confirmed_by_the_acknowledgement(monkeypatch, tmp_path, bridge_env, no_reload):
    """Live runs bdea5e90/b5343990: a trial FL cannot save, and every write
    came back "unconfirmed" while reads showed the stale file. The ack is the
    confirmation: the write is definitive, recorded, and read back at once."""
    project = project_flp(monkeypatch, tmp_path, file_notes=1)
    saves: list[bool] = []
    monkeypatch.setattr(notes_mod.controls, "save_project", lambda session: saves.append(True))
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=project, fl_license=FLLicense.TRIAL
    )

    merged = session.write_notes(TARGET, [Note(key=64, start=1, length=1)], "merge")

    assert merged.total == 2, "the file's one note plus the merged one"
    assert [n.key for n in session.read_notes(TARGET)] == [60, 64]
    replaced = session.write_notes(TARGET, NOTES, "replace")
    assert (replaced.wrote, replaced.total) == (1, 1)
    assert session.read_notes(TARGET) == NOTES
    assert saves == [], "a trial FL is never asked to save"


def test_a_save_after_the_write_supersedes_the_record(monkeypatch, tmp_path, bridge_env, no_reload):
    """A person's Cmd+S (or any save of FL's live state) postdates the record:
    the saved project then holds what FL has, their edits included."""
    project = project_flp(monkeypatch, tmp_path, file_notes=1)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=project, fl_license=FLLicense.TRIAL
    )
    three = [Note(key=60 + i, start=i, length=1) for i in range(3)]

    session.write_notes(TARGET, three, "replace")
    assert session.read_notes(TARGET) == three, "the record is newer than the loaded project"

    later = time.time() + 5
    os.utime(project, (later, later))
    assert len(session.read_notes(TARGET)) == 1, "the newer save wins"


def test_bridge_writes_without_a_project_file_count_from_this_sessions_writes(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """No .flp to read (create_session leaves project None): Bridge writes
    still land, a replace is fully known, and later reads count from it."""
    monkeypatch.setattr(flp, "read", lambda *_args: pytest.fail("no project must be opened"))
    snapshot = {"tempo": 100000, "ppq": 96, "playing": False, "current_pattern": 1, "mixer": [],
                "channels": [{"idx": 0, "name": "Kick", "vol": 0.78, "pan": 0.0, "muted": False, "target_mixer_track": 1}]}
    control = FakeControl(
        hello=HELLO, handlers={"channels.selectedChannel": lambda args: 0, "state.snapshot": lambda args: snapshot}
    )
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(control=control, transport=spool, project=None)

    first = session.write_notes(TARGET, NOTES, "replace")
    second = session.write_notes(TARGET, [Note(key=64, start=1, length=1)], "merge")

    assert [first.total, second.total] == [1, 2]
    assert second.track == "Kick"
    assert [n.key for n in session.read_notes(TARGET)] == [60, 64]
    assert session.write_notes(Target(pattern=2, channel=0), NOTES, "merge").total is None


def test_bridge_rewrite_saves_live_readback_for_replace_and_merge(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """Regression: existing live notes must not leak into a stale result.

    The fake bridge response deliberately lies about its count. Each save
    exposes FL's post-write state: replace removes the legacy note, then merge
    retains the replacement and adds its note.
    """
    existing = FlpNote(position=0, length=96, key=48, channel=0, velocity=100, pan=64)
    replaced = FlpNote(position=0, length=96, key=60, channel=0, velocity=100, pan=64)
    merged = FlpNote(position=96, length=96, key=64, channel=0, velocity=100, pan=64)
    post_save = [[existing], [replaced], [replaced, merged]]
    saves: list[int] = []
    monkeypatch.setattr(flp, "read", lambda path: SimpleNamespace(ppq=96, channels=()))
    monkeypatch.setattr(flp, "notes_at", lambda path, pattern, channel: post_save[len(saves)])
    monkeypatch.setattr(notes_mod.controls, "save_project", lambda session: saves.append(1))
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=tmp_path / "agent.flp"
    )
    assert session.project is not None
    session.project.write_bytes(b"FLhd")
    session.observer._baseline = {
        (1, 0): [Note(key=existing.key, start=0, length=1, velocity=existing.velocity / 127, pan=0.5)]
    }
    session.observer._baseline_mtime_ns = session.project.stat().st_mtime_ns

    replaced_result = session.write_notes(TARGET, [Note(key=60, start=0, length=1)], "replace")
    merged_result = session.write_notes(TARGET, [Note(key=64, start=1, length=1)], "merge")

    assert [replaced_result.total, merged_result.total] == [1, 2]
    assert saves == [1, 1]
    assert session.observer._baseline[(1, 0)] == [
        Note(key=60, start=0, length=1, velocity=100 / 127, pan=0.5),
        Note(key=64, start=1, length=1, velocity=100 / 127, pan=0.5),
    ]


def test_ppq_stamp_falls_back_to_hello_when_file_unreadable(monkeypatch, tmp_path, bridge_env, no_reload):
    def unreadable(path):
        raise flp.FlpError("torn header")

    monkeypatch.setattr(flp, "read", unreadable)
    monkeypatch.setattr(flp, "notes_at", lambda path, pattern, channel: [])
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=template_copy(tmp_path)
    )

    session.write_notes(TARGET, NOTES, "merge")

    assert spool.sent[-1][1]["ppq"] == HELLO.ppq


# -- the heartbeat parser fails closed ----------------------------------------


def test_heartbeat_missing_file_fails_closed():
    hb = read_heartbeat(Path("/nonexistent/heartbeat.txt"))
    assert hb.age == math.inf and hb.at is None and hb.proto is None


def test_heartbeat_garbage_fails_closed(tmp_path):
    junk = tmp_path / "hb.txt"
    junk.write_text("unix_time=not-a-number\nproto=soon\nnoise\n", encoding="utf-8")
    hb = read_heartbeat(junk)
    assert hb.age == math.inf and hb.proto is None


def test_heartbeat_minus_one_context_means_unknown(tmp_path):
    """L4's contract: chan=-1 ALWAYS (no FL getter exists), pat=-1 until the
    bridge's first live write. -1 maps to None, never to a real coordinate."""
    hb_file = tmp_path / "hb.txt"
    write_heartbeat(hb_file)
    hb = read_heartbeat(hb_file)
    assert hb.proto == 2 and hb.pattern is None and hb.channel is None
    hb_file.write_text("unix_time=5\nproto=2\npat=7\nchan=0\n", encoding="utf-8")
    hb = read_heartbeat(hb_file)
    assert hb.pattern == 7 and hb.channel == 0


# -- BridgeBreaker unit tests (docs/27 §3) ------------------------------------


class ScriptedPing:
    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.results.pop(0)


def test_breaker_first_use_is_ping_gated():
    ping = ScriptedPing(True)
    breaker = BridgeBreaker(ping)
    assert breaker.closed is False, "half_open until a ping proves the pumps run"
    assert breaker.admit(None) is True
    assert breaker.closed is True
    assert ping.calls == 1
    assert breaker.admit(None) is True, "closed trusts the heartbeat, no re-ping"
    assert ping.calls == 1


def test_breaker_soft_failure_re_arms_the_ping_gate():
    ping = ScriptedPing(True, True)
    breaker = BridgeBreaker(ping)
    breaker.admit(None)
    breaker.record_soft_failure()  # a timeout mid-command
    assert breaker.closed is False
    assert breaker.admit(None) is True, "one ping round trip re-closes"
    assert ping.calls == 2


def test_breaker_opens_after_three_soft_failures_and_reprobes_rate_limited():
    clock = [1000.0]
    ping = ScriptedPing(False, False, False, True)
    breaker = BridgeBreaker(ping, clock=lambda: clock[0])
    for _ in range(3):
        assert breaker.admit(heartbeat_at=2000.0) is False
    assert ping.calls == 3, "three failed probes open the breaker"
    # Open: a newer heartbeat alone is not enough inside the probe interval -
    # a wedged bridge keeps ticking heartbeats (R4 B2-7).
    assert breaker.admit(heartbeat_at=2000.0) is False
    assert ping.calls == 3
    clock[0] += REOPEN_PROBE_MIN_INTERVAL_SECONDS + 1
    assert breaker.admit(heartbeat_at=2000.0) is True
    assert ping.calls == 4 and breaker.closed is True


def test_breaker_open_needs_a_heartbeat_newer_than_the_failure():
    clock = [1000.0]
    breaker = BridgeBreaker(ScriptedPing(True), clock=lambda: clock[0])
    breaker.record_success()
    breaker.record_hard_failure("add_notes command 7 unacked; FL not running")
    assert breaker.closed is False
    clock[0] += REOPEN_PROBE_MIN_INTERVAL_SECONDS + 1
    assert breaker.admit(heartbeat_at=999.0) is False, "evidence predates the failure"
    assert breaker.admit(heartbeat_at=None) is False
    assert breaker.admit(heartbeat_at=1001.0) is True, "a NEW heartbeat re-arms the probe"
    assert breaker.last_hard_fingerprint == "add_notes command 7 unacked; FL not running"


# -- routing notes through core.router.route with the REAL lanes' can_serve --

NO_TARGETING = SimpleNamespace(capabilities=SimpleNamespace(channel_targeting_live=False))


def caps(**overrides) -> Capabilities:
    base = {
        "bridge_live": True,
        "bridge_pattern": None,
        "bridge_channel": 0,
        "profile": NO_TARGETING,
        "bridge_heartbeat_at": 12345.0,
    }
    base.update(overrides)
    return Capabilities(**base)


def pick(capabilities: Capabilities, breaker: BridgeBreaker) -> str:
    """route() over the real lanes' pure can_serve predicates (no session:
    can_serve never touches it); returns the chosen lane's name."""
    op = ops_notes.WRITE.at(Target(pattern=1, channel=0))
    lanes = [BridgeLane(None), FileLane(None)]  # type: ignore[arg-type]
    return route(op, capabilities, lanes, breaker).name


def test_pick_routes_bridge_only_on_full_evidence():
    breaker = BridgeBreaker(ScriptedPing(True, True))
    assert pick(caps(), breaker) == "bridge"

    assert pick(caps(bridge_live=False), breaker) == "project-write"
    assert pick(caps(bridge_channel=3), breaker) == "project-write"
    assert pick(caps(bridge_channel=None), breaker) == "project-write"


def test_pick_never_pings_when_cheaper_evidence_already_fails():
    ping = ScriptedPing()  # any call would raise IndexError
    breaker = BridgeBreaker(ping)
    assert pick(caps(bridge_live=False), breaker) == "project-write"
    assert pick(caps(bridge_channel=1), breaker) == "project-write"
    assert ping.calls == 0


def test_pick_honors_profile_channel_targeting():
    breaker = BridgeBreaker(ScriptedPing(True))
    assert pick(caps(bridge_channel=3, profile=LIVE_PROFILE), breaker) == "bridge"
    assert pick(caps(bridge_channel=None, profile=LIVE_PROFILE), breaker) == "project-write"
