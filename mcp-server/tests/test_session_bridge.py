"""The bridge lane rides capability detection + a circuit breaker - no flag.

FL_MCP_BRIDGE is DEAD (docs/27 §2, doctrine #54). Eligibility is evidence,
per call: a fresh proto-matched heartbeat, a wire-verified selected channel
(the bridge cannot see its own channel - heartbeat chan= is always -1,
VERDICT Q2), and a ping-gated breaker. Receipts never trust the bridge's
echo; test_session_notes.py stays authoritative for the file lane.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from session_fakes import (
    HELLO,
    NOTES,
    TARGET,
    FakeControl,
    FakeSpool,
    make_session,
)

from fl_mcp import errors, flp
from fl_mcp.core.router import route
from fl_mcp.ops import notes as ops_notes
from fl_mcp.schemas import SpoolResponse, Target
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

PING_OK = SpoolResponse(id=0, ok=True, op="ping")
BRIDGE_OK = SpoolResponse(id=0, ok=True, op="add_notes", notes_written=999)  # 999: a lying echo

LIVE_PROFILE = SimpleNamespace(capabilities=SimpleNamespace(channel_targeting_live=True))


def write_heartbeat(path: Path, *, unix_time: int | None = None, proto: int | None = 2) -> None:
    """A heartbeat as L4's bridge writes it (docs/27 §6): chan=-1 always (no
    FL getter for the selected channel exists), pat=-1 until a first write."""
    lines = ["tick=12", "applied=0", "pid=1", "backend=bridge"]
    lines.append(f"unix_time={int(time.time()) if unix_time is None else unix_time}")
    if proto is not None:
        lines.append(f"proto={proto}")
    lines += ["pat=-1", "chan=-1"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    monkeypatch.setattr(flp, "read", lambda path: SimpleNamespace(ppq=ppq, notes=[]))
    monkeypatch.setattr(flp, "notes_at", lambda path, pattern, channel: [object()] * file_notes)

    def never(*a, **k):
        raise AssertionError("the bridge path must not write the project file")

    monkeypatch.setattr(flp, "write_notes", never)
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    return project


# -- eligibility: evidence in, lane out ---------------------------------------


def test_fresh_heartbeat_and_matching_channel_routes_bridge(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    project = project_flp(monkeypatch, tmp_path, ppq=192, file_notes=2)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, store = make_session(
        control=selected_channel_control(0), transport=spool, project=project
    )

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "bridge"
    # Ping-gated first use, then the spooled command carries the project's
    # ppq; the consumer never guesses.
    assert spool.sent == [
        ("ping", {}),
        ("add_notes", {"target": TARGET, "notes": NOTES, "mode": "merge", "ppq": 192}),
    ]
    # Readback-only: the count comes from the file read, NEVER the echo (999).
    assert receipt.confirmed_notes == 2
    # Recorded against the spool command id, and FL was never reloaded (the
    # write landed live; a reload would destroy it).
    assert store.writes == [(TARGET, NOTES, "merge", "bridge", 2)]
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

    receipt = session.clear_notes(TARGET)

    assert receipt.backend == "bridge" and receipt.confirmed_notes == 0
    assert spool.sent == [("ping", {}), ("clear_notes", {"target": TARGET})]
    assert store.writes == [(TARGET, [], "replace", "bridge", 2)]
    assert no_reload == []


def test_stale_heartbeat_routes_project_write(monkeypatch, tmp_path, bridge_env, no_reload):
    write_heartbeat(bridge_env, unix_time=int(time.time() - 3600))
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=tmp_path / "agent.flp"
    )

    assert session.write_notes(TARGET, NOTES, "merge").backend == "project-write"
    assert spool.sent == [], "a dead bridge must never receive commands"


@pytest.mark.parametrize("proto", [None, 1], ids=["missing", "mismatched"])
def test_unspoken_heartbeat_proto_routes_project_write(
    monkeypatch, tmp_path, bridge_env, no_reload, proto
):
    """A heartbeat whose protocol the server does not speak is refused -
    exactly analogous to the wire's dropped-frames codec check."""
    write_heartbeat(bridge_env, proto=proto)
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=tmp_path / "agent.flp"
    )

    assert session.write_notes(TARGET, NOTES, "merge").backend == "project-write"
    assert spool.sent == []


def test_selected_channel_mismatch_routes_project_write(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """VERDICT Q2: a bridge write lands in the SELECTED channel, whatever the
    target says - so a mismatch must route to the file lane, not write wrong."""
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool(responses={"ping": PING_OK})
    session, _ = make_session(
        control=selected_channel_control(3), transport=spool, project=tmp_path / "agent.flp"
    )

    assert session.write_notes(TARGET, NOTES, "merge").backend == "project-write"
    assert spool.sent == [], "not even a ping - the channel constraint failed first"


def test_wire_down_cannot_prove_the_channel_routes_project_write(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """R2 §3.2: the bridge cannot see its own channel (heartbeat chan=-1) and
    with the wire down nobody can - correctness beats liveness."""
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool(responses={"ping": PING_OK})
    session, _ = make_session(control=None, transport=spool, project=tmp_path / "agent.flp")

    assert session.write_notes(TARGET, NOTES, "merge").backend == "project-write"
    assert spool.sent == []


def test_profile_live_channel_targeting_widens_eligibility(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """When a profile reports live channel selection (R2 select-then-write),
    the channel constraint dissolves; only the predicate widens (docs/27 §2)."""
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(3),  # mismatch - irrelevant now
        transport=spool,
        project=project,
        profile=LIVE_PROFILE,
    )

    assert session.write_notes(TARGET, NOTES, "merge").backend == "bridge"


# -- the breaker at the session seam ------------------------------------------


def test_prewarm_closes_the_breaker_before_the_first_bridge_write(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=project
    )

    session.prewarm()

    assert session.breaker.closed is True
    assert session.write_notes(TARGET, NOTES, "merge").backend == "bridge"
    assert [op for op, _ in spool.sent] == ["ping", "add_notes"]


def test_failed_prewarm_keeps_the_first_write_on_the_file_lane(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()  # no ping response: the bridge cannot prove its pumps run
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=tmp_path / "agent.flp"
    )

    session.prewarm()

    assert session.breaker.closed is False
    assert session.write_notes(TARGET, NOTES, "merge").backend == "project-write"
    assert [op for op, _ in spool.sent] == ["ping", "ping"]


def test_failed_ping_falls_back_to_file_for_that_call(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    """R4 B2-7's failure mode: a wedged GUI thread keeps writing heartbeats
    while pumps never run. The ping gate catches it and the call lands on the
    file lane instead of burning the command timeout."""
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()  # no scripted ping response: the round trip times out
    session, store = make_session(
        control=selected_channel_control(0), transport=spool, project=tmp_path / "agent.flp"
    )

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "project-write"
    assert [op for op, _ in spool.sent] == ["ping"]
    assert store.writes == [(TARGET, NOTES, "merge", "project-write", 0)]
    assert session.breaker.closed is False


def test_bridge_refusal_raises_and_records_nothing(monkeypatch, tmp_path, bridge_env, no_reload):
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(
        responses={
            "ping": PING_OK,
            "add_notes": SpoolResponse(id=0, ok=False, op="add_notes", err="no plugin host"),
        }
    )
    session, store = make_session(control=selected_channel_control(0), transport=spool, project=project)

    with pytest.raises(WriteRejected, match="no plugin host"):
        session.write_notes(TARGET, NOTES, "merge")
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


def test_bridge_timeout_with_fl_dead_is_a_hard_failure(
    monkeypatch, tmp_path, bridge_env, no_reload
):
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


def test_bridge_readback_failure_is_unconfirmed(monkeypatch, tmp_path, bridge_env, no_reload):
    monkeypatch.setattr(flp, "read", lambda path: SimpleNamespace(ppq=96))

    def torn(path, pattern, channel):
        raise flp.FlpError("torn header")

    monkeypatch.setattr(flp, "notes_at", torn)
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=tmp_path / "agent.flp"
    )

    with pytest.raises(WriteRejected, match="UNCONFIRMED"):
        session.write_notes(TARGET, NOTES, "merge")


def test_ppq_stamp_falls_back_to_hello_when_file_unreadable(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    def unreadable(path):
        raise flp.FlpError("torn header")

    monkeypatch.setattr(flp, "read", unreadable)
    monkeypatch.setattr(flp, "notes_at", lambda path, pattern, channel: [])
    spool = FakeSpool(responses={"ping": PING_OK, "add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=selected_channel_control(0), transport=spool, project=tmp_path / "agent.flp"
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
    assert pick(caps(bridge_channel=None, profile=LIVE_PROFILE), breaker) == "bridge"
