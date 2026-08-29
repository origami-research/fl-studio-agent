"""v2 session: explicit backend choice, readback receipts, honest failures.

The wire and store are injected fakes (their real implementations are other
lanes' work); the spool is the real one where the protocol matters. The flp
module is patched at its module seam for the project-write backend - Lane F's
real implementation slots in without changing these tests' claims, and the
live gates (G3/G4) exercise the unpatched path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from fl_mcp import errors, flp
from fl_mcp import session as session_mod
from fl_mcp.clock import now
from fl_mcp.schemas import (
    ControlResponse,
    HelloEvent,
    Note,
    NoteDelta,
    NoteSetRef,
    SpoolCommand,
    SpoolResponse,
    Target,
)
from fl_mcp.session import FLSession, NotePlaneUnavailable, WriteRejected, create_session
from fl_mcp.spool import Spool, write_json_atomic
from fl_mcp.store import Store
from fl_mcp.wire import MidiControl

HELLO = HelloEvent(script_ver="test", fl_ver="2026", ppq=96)
TARGET = Target(pattern=1, channel=0)
NOTES = [Note(key=60, start=0, length=1)]


class FakeControl:
    """The MidiControl surface the session consumes, with scripted answers."""

    def __init__(
        self,
        hello: HelloEvent | None = None,
        handlers: dict[str, Any] | None = None,
        dropped: int = 0,
    ) -> None:
        self.hello = hello
        self.handlers = handlers or {}
        self.dropped_frames = dropped
        self.calls: list[tuple[str, list[Any]]] = []

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        if self.hello is None:
            raise errors.control_plane_unavailable("hello", timeout)
        return self.hello

    def send_request(
        self,
        op: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> ControlResponse:
        self.calls.append((op, args or []))
        if op not in self.handlers:
            return ControlResponse(id=len(self.calls), ok=False, err=f"no handler for {op}")
        ret = self.handlers[op](args or [])
        # A handler may script a full failure response (e.g. unsafe-at-current-time).
        if isinstance(ret, ControlResponse):
            return ret
        return ControlResponse(id=len(self.calls), ok=True, ret=ret)


class FakeSpool:
    """Session<->spool contract without the filesystem: scripted responses."""

    def __init__(self, responses: dict[str, SpoolResponse | Exception] | None = None) -> None:
        self.responses = responses or {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._id = 0

    def send(self, op: str, **args: Any) -> SpoolCommand:
        self._id += 1
        self.sent.append((op, args))
        return SpoolCommand(id=self._id, op=op, args={})

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        outcome = self.responses.get(command.op)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise errors.note_plane_unavailable(command.id, timeout)
        return outcome


class FakeStore:
    def __init__(self) -> None:
        self.writes: list[tuple[Target, list[Note], str, str, int]] = []
        self.ground_truths: list[tuple[Target, list[Note]]] = []
        self.preloaded: dict[tuple[int, int], list[Note]] = {}

    def record_write(self, target, notes, mode, backend, command_id) -> NoteDelta:
        self.writes.append((target, notes, mode, backend, command_id))
        return NoteDelta(added=list(notes))

    def notes_at(self, target) -> list[Note] | None:
        return self.preloaded.get((target.pattern, target.channel))

    def mark_ground_truth(self, target, notes) -> NoteSetRef:
        self.ground_truths.append((target, notes))
        return NoteSetRef(id="gt", target=target, provenance="project", as_of=now())


def make_session(
    *,
    control: FakeControl | None = None,
    spool: FakeSpool | Spool | None = None,
    store: FakeStore | None = None,
    project: Path | None = None,
) -> tuple[FLSession, FakeStore]:
    store = store or FakeStore()
    # The fakes implement exactly the surface FLSession consumes; the casts are
    # honest because every method the session calls exists on the doubles.
    session = FLSession(
        control=cast(MidiControl, control),
        spool=cast(Spool, spool or FakeSpool()),
        store=cast(Store, store),
        project=project,
    )
    return session, store


@pytest.fixture()
def no_reload(monkeypatch):
    """Never let a unit test SIGKILL or launch FL; record the calls instead."""
    calls: list[bool] = []
    monkeypatch.setattr(FLSession, "_reload_fl", lambda self: calls.append(True))
    return calls


# --- write_notes: the bridge (spool) backend --------------------------------


def test_bridge_write_confirms_readback_and_records_delta():
    spool = FakeSpool(
        {"add_notes": SpoolResponse(id=1, ok=True, op="add_notes", notes_written=5)}
    )
    session, store = make_session(control=FakeControl(hello=HELLO), spool=spool)

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "bridge"
    assert receipt.confirmed_notes == 5, "receipt must carry the READBACK, not the request"
    assert receipt.delta.added == NOTES
    assert store.writes == [(TARGET, NOTES, "merge", "bridge", 1)]
    assert spool.sent[0][0] == "add_notes"


def test_bridge_rejection_raises_and_records_nothing():
    spool = FakeSpool({"add_notes": SpoolResponse(id=1, ok=False, op="add_notes", err="boom")})
    session, store = make_session(control=FakeControl(hello=HELLO), spool=spool)

    with pytest.raises(WriteRejected, match="boom"):
        session.write_notes(TARGET, NOTES, "merge")
    assert store.writes == []


def test_bridge_timeout_propagates_and_never_falls_through(no_reload):
    """v1's cascade would fall to another backend after a timeout; v2 must not:
    the command may still exist, and a second application path is the partial-
    success bug this rewrite exists to kill."""
    session, store = make_session(
        control=FakeControl(hello=HELLO), spool=FakeSpool(), project=Path("/tmp/agent.flp")
    )

    with pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")

    assert excinfo.value.category == "note_plane_unavailable"
    assert store.writes == []
    assert no_reload == [], "must not reload FL after choosing the live plane"


def test_bridge_write_over_the_real_spool(tmp_path):
    """End-to-end through the real file protocol, with the consumer's response
    planted the way the pyscript writes it."""
    spool = Spool(root=tmp_path)
    write_json_atomic(
        tmp_path / "responses" / "000001.json",
        {"id": 1, "ok": True, "op": "add_notes", "notes_written": 1},
    )
    session, store = make_session(control=FakeControl(hello=HELLO), spool=spool)

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "bridge"
    assert receipt.confirmed_notes == 1
    assert store.writes[0][4] == 1  # the spool command id, for the action log


# --- write_notes: the project-write backend ---------------------------------


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


def test_clear_notes_bridge_confirms_empty():
    spool = FakeSpool(
        {"clear_notes": SpoolResponse(id=1, ok=True, op="clear_notes", notes_written=0)}
    )
    session, store = make_session(control=FakeControl(hello=HELLO), spool=spool)

    receipt = session.clear_notes(TARGET)

    assert receipt.confirmed_notes == 0
    assert store.writes == [(TARGET, [], "replace", "bridge", 1)]


# --- read_notes --------------------------------------------------------------


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


# --- set_tempo ---------------------------------------------------------------


def test_set_tempo_live_returns_the_readback_not_the_request():
    control = FakeControl(
        hello=HELLO,
        handlers={
            "mixer.setCurrentTempo": lambda args: True,
            "mixer.getCurrentTempo": lambda args: 128500,  # BPM x 1000, clamped by FL
        },
    )
    session, _ = make_session(control=control)

    assert session.set_tempo(130.0) == 128.5
    assert ("mixer.setCurrentTempo", [130.0, 1]) in control.calls


def test_set_tempo_live_unconfirmed_raises():
    control = FakeControl(hello=HELLO, handlers={"mixer.setCurrentTempo": lambda args: True})
    session, _ = make_session(control=control)

    with pytest.raises(WriteRejected, match="UNCONFIRMED"):
        session.set_tempo(130.0)


def test_set_tempo_project_backend(monkeypatch, tmp_path, no_reload):
    monkeypatch.setattr(flp, "set_tempo", lambda path, bpm: 137.0)
    session, _ = make_session(project=tmp_path / "agent.flp")

    assert session.set_tempo(137.0) == 137.0
    assert no_reload == [True]


def test_set_tempo_no_backend_raises():
    session, _ = make_session()
    with pytest.raises(NotePlaneUnavailable):
        session.set_tempo(120.0)


# --- set_channel_levels ------------------------------------------------------


def test_levels_live_all_fields_with_readback():
    control = FakeControl(
        hello=HELLO,
        handlers={
            "channels.setChannelVolume": lambda args: True,
            "channels.setChannelPan": lambda args: True,
            "channels.setChannelPitch": lambda args: True,
            "channels.getChannelVolume": lambda args: 0.5,
            "channels.getChannelPan": lambda args: -0.25,
            "channels.getChannelPitch": lambda args: 12.0,
        },
    )
    session, _ = make_session(control=control)

    levels = session.set_channel_levels(2, volume=0.6, pan=-0.3, pitch_semitones=12)

    # The result is FL's readback, not an echo of the request.
    assert (levels.volume, levels.pan, levels.pitch_semitones) == (0.5, -0.25, 12)
    assert ("channels.setChannelPitch", [2, 12, 1]) in control.calls


def test_levels_live_partial_failure_raises_and_never_falls_through(
    monkeypatch, tmp_path, no_reload
):
    """The v1 bug this lane exists to fix: volume lands live, pan fails, and the
    fallback reloads FL from the file - silently discarding the volume. v2
    raises instead, naming what was kept."""
    monkeypatch.setattr(
        flp,
        "set_channel_levels",
        lambda *a, **k: pytest.fail("must not fall through to the file backend"),
    )
    control = FakeControl(hello=HELLO, handlers={"channels.setChannelVolume": lambda args: True})
    session, _ = make_session(control=control, project=tmp_path / "agent.flp")

    with pytest.raises(WriteRejected, match="KEPT: volume"):
        session.set_channel_levels(2, volume=0.6, pan=-0.3)
    assert no_reload == []


def test_levels_project_backend(monkeypatch, tmp_path, no_reload):
    from fl_mcp.schemas import ChannelLevels

    confirmed = ChannelLevels(channel=2, volume=0.6, pan=-0.3, pitch_semitones=0)
    monkeypatch.setattr(flp, "set_channel_levels", lambda *a, **k: confirmed)
    session, _ = make_session(project=tmp_path / "agent.flp")

    assert session.set_channel_levels(2, volume=0.6, pan=-0.3) == confirmed
    assert no_reload == [True]


def test_levels_require_at_least_one_field():
    session, _ = make_session(control=FakeControl(hello=HELLO))
    with pytest.raises(ValueError, match="nothing to change"):
        session.set_channel_levels(2)


# --- snapshot ----------------------------------------------------------------


def test_snapshot_from_project_file(monkeypatch, tmp_path):
    channel = flp.FlpChannel(index=0, name="Lead", volume=10000, pan=6400, pitch_semitones=0)
    monkeypatch.setattr(
        flp,
        "read",
        lambda path: flp.FlpProject(ppq=192, tempo=140.0, channels=(channel,), notes=()),
    )
    session, _ = make_session(project=tmp_path / "agent.flp")

    state = session.snapshot()

    assert state.ppq == 192
    assert state.tempo_bpm == 140.0
    assert state.channels[0].name == "Lead"
    assert state.channels[0].volume == pytest.approx(0.78125)
    assert state.channels[0].pan == 0.0


SNAPSHOT_PAYLOAD = {
    "ppq": 96,
    "tempo": 128000,  # getCurrentTempo: BPM x 1000
    "playing": True,
    "song_pos": 0.0,
    "current_pattern": 3,
    "pattern_count": 4,
    "channels": [
        {"idx": 0, "name": "Kick", "muted": False, "vol": 0.78, "pan": 0.0, "target_mixer_track": 1}
    ],
    "mixer": [
        {"idx": 0, "name": "Master", "vol": 0.8, "pan": 0.0, "muted": False, "solo": False},
        {"idx": 1, "name": "Kick bus", "vol": 0.7, "pan": -0.1, "muted": True, "solo": False},
    ],
}


def test_snapshot_live_uses_one_composite_round_trip(monkeypatch):
    """Addendum A5: state.snapshot works in v2 (Lane W fixed chunk routing);
    the session must NOT read the file when the live plane answers."""
    monkeypatch.setattr(
        flp, "read", lambda path: pytest.fail("live snapshot must not touch the file")
    )
    control = FakeControl(hello=HELLO, handlers={"state.snapshot": lambda args: SNAPSHOT_PAYLOAD})
    session, _ = make_session(control=control, project=Path("/tmp/agent.flp"))

    state = session.snapshot()

    assert control.calls == [("state.snapshot", [])], "exactly one composite round trip"
    assert state.tempo_bpm == 128.0
    assert state.ppq == 96
    assert state.playing is True
    assert state.current_pattern == 3
    assert [c.name for c in state.channels] == ["Kick"]
    assert [t.name for t in state.mixer] == ["Master", "Kick bus"]
    assert state.mixer[1].muted is True


def test_snapshot_live_malformed_payload_is_a_typed_error():
    control = FakeControl(hello=HELLO, handlers={"state.snapshot": lambda args: {"ppq": 96}})
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as excinfo:
        session.snapshot()
    assert excinfo.value.category == "fl_api_error"
    assert "malformed" in excinfo.value.message


# --- play / stop / undo (addendum A11) ---------------------------------------

UNSAFE = ControlResponse(id=1, ok=False, err="Operation unsafe at current time (mid-cycle)")


@pytest.mark.parametrize(
    ("method", "op"),
    [("play", "transport.start"), ("stop", "transport.stop"), ("undo", "general.undo")],
)
def test_transport_methods_send_their_op_and_return_none(method, op):
    control = FakeControl(hello=HELLO, handlers={op: lambda args: 0})
    session, _ = make_session(control=control)

    assert getattr(session, method)() is None
    assert control.calls == [(op, [])]


@pytest.mark.parametrize("method", ["play", "stop", "undo"])
def test_transport_methods_raise_without_a_control_plane(method):
    session, _ = make_session()
    with pytest.raises(errors.ToolError) as excinfo:
        getattr(session, method)()
    assert excinfo.value.category == "control_plane_unavailable"


def test_transport_unsafe_refusal_is_retryable_operation_unsafe():
    """Addendum A6: unsafe-at-current-time means busy, not broken."""
    control = FakeControl(hello=HELLO, handlers={"transport.start": lambda args: UNSAFE})
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as excinfo:
        session.play()
    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable is True


def test_transport_other_refusals_surface_fl_detail():
    boom = ControlResponse(id=1, ok=False, err="no such op")
    control = FakeControl(hello=HELLO, handlers={"general.undo": lambda args: boom})
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as excinfo:
        session.undo()
    assert excinfo.value.category == "fl_api_error"
    assert "no such op" in excinfo.value.message


def test_set_tempo_unsafe_refusal_never_falls_through(monkeypatch, tmp_path, no_reload):
    """The warm-up window after _reload_fl (A6): degrade to the file backend
    here and you have rebuilt the v1 cascade bug."""
    monkeypatch.setattr(
        flp, "set_tempo", lambda *a: pytest.fail("must not fall through to the file backend")
    )
    control = FakeControl(hello=HELLO, handlers={"mixer.setCurrentTempo": lambda args: UNSAFE})
    session, _ = make_session(control=control, project=tmp_path / "agent.flp")

    with pytest.raises(errors.ToolError) as excinfo:
        session.set_tempo(130.0)
    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.retryable is True
    assert no_reload == []


# --- doctor ------------------------------------------------------------------


def test_doctor_all_degraded_names_a_fix_per_plane(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    session, _ = make_session()

    report = session.doctor()

    assert report.status == "degraded"
    assert report.control_plane.connected is False
    assert "python-rtmidi" in report.control_plane.fix
    assert report.note_plane.connected is False
    assert report.note_plane.fix
    assert report.project_file.connected is False
    assert "make_agent_project" in report.project_file.fix


def test_doctor_all_connected(tmp_path, monkeypatch):
    settings = tmp_path / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    scripts = settings / "Piano roll scripts"
    scripts.mkdir(parents=True)
    (scripts / "MCP Write Notes.pyscript").write_text("# installed", encoding="utf-8")
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    session, _ = make_session(control=FakeControl(hello=HELLO, dropped=3), project=project)
    report = session.doctor()

    assert report.status == "ok"
    assert report.control_plane.connected is True
    assert "dropped_frames=3" in report.control_plane.detail
    assert report.note_plane.connected is True
    assert report.project_file.detail == str(project)


def test_doctor_dropped_frames_without_hello_points_at_the_codec(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    session, _ = make_session(control=FakeControl(hello=None, dropped=17))

    report = session.doctor()

    assert report.control_plane.connected is False
    assert "17" in report.control_plane.detail
    assert "codec" in report.control_plane.detail or "codec" in report.control_plane.fix


def test_doctor_port_ordering_fix_when_fl_runs_without_hello(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(session_mod, "fl_process_running", lambda: True)
    session, _ = make_session(control=FakeControl(hello=None))

    report = session.doctor()

    assert report.control_plane.connected is False
    assert "restart" in report.control_plane.fix.lower()


# --- create_session ----------------------------------------------------------


def test_create_session_is_the_factory():
    """Turns fully green once Lanes W (wire) and K (store) land; until then the
    stubs' NotImplementedError proves create_session reached the real deps."""
    try:
        session = create_session()
    except NotImplementedError as exc:
        pytest.skip(f"blocked on another lane's stub: {exc}")
    assert isinstance(session, FLSession)


def test_bridge_write_fires_the_injected_trigger_best_effort():
    """The auto-trigger (FL's 'repeat last script' keystroke) fires after the
    command is queued, and a failing trigger never fails the write - it is an
    accelerator; the human can still run the script manually."""
    spool = FakeSpool(
        {"add_notes": SpoolResponse(id=1, ok=True, op="add_notes", notes_written=1)}
    )
    fired: list[bool] = []
    session, _ = make_session(control=FakeControl(hello=HELLO), spool=spool)
    session._trigger = lambda: fired.append(True)
    session.write_notes(TARGET, NOTES, "merge")
    assert fired == [True]

    def boom() -> None:
        raise RuntimeError("no accessibility consent")

    session._trigger = boom
    receipt = session.write_notes(TARGET, NOTES, "merge")  # must not raise
    assert receipt.backend == "bridge"
