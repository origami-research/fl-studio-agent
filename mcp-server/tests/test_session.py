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
from fl_mcp.spool import Spool
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
        self.closed = False

    def close(self) -> None:
        self.closed = True

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
        if op == "ping" and "ping" not in self.handlers:
            # Like the real controller: ping answers whenever the wire is alive,
            # which for the fake means "whenever a hello was scripted".
            return ControlResponse(id=len(self.calls), ok=self.hello is not None, err=None if self.hello else "dead wire")
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
        # NEVER the real MidiControl in the fast suite (real ports + open FL
        # segfault rtmidi); a recycle in a test yields another silent fake.
        control_factory=cast(Any, lambda: FakeControl(hello=None)),
    )
    return session, store


@pytest.fixture()
def no_reload(monkeypatch):
    """Never let a unit test SIGKILL or launch FL; record the calls instead."""
    calls: list[bool] = []
    monkeypatch.setattr(FLSession, "_reload_fl", lambda self: calls.append(True))
    return calls


# --- write_notes: targeted notes are ALWAYS project-write ------------------
#
# Dogfood-found (docs/21 A12): the pyscript/spool lane is TARGET-BLIND - a
# piano-roll script edits whichever score is open, so three "targeted" writes
# all landed in one roll while their receipts claimed targets. A connected
# control plane must therefore NOT divert note writes off the file.


def test_notes_go_project_write_even_when_control_is_connected(monkeypatch, tmp_path, no_reload):
    project = tmp_path / "agent.flp"
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, store = make_session(
        control=FakeControl(hello=HELLO), spool=spool, project=project
    )

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "project-write"
    assert spool.sent == [], "the target-blind pyscript lane must never see a targeted write"
    assert store.writes == [(TARGET, NOTES, "merge", "project-write", 0)]
    assert no_reload == [True]


def test_notes_without_a_project_raise_even_when_control_is_connected():
    session, store = make_session(control=FakeControl(hello=HELLO), project=None)

    with pytest.raises(NotePlaneUnavailable):
        session.write_notes(TARGET, NOTES, "merge")
    assert store.writes == []


def test_recording_failure_never_fails_an_applied_write(monkeypatch, tmp_path, no_reload, caplog):
    """Found live: the store crashed AFTER the file write landed, so every
    applied write reported a traceback. Recording is memory - it logs and
    yields an empty delta, and the receipt still reports the readback."""
    project = tmp_path / "agent.flp"
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    session, store = make_session(project=project)

    def boom(*a, **k):
        raise OSError("store root unwritable")

    store.record_write = boom
    with caplog.at_level("ERROR"):
        receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "project-write" and receipt.confirmed_notes == 1
    assert receipt.delta.is_empty()
    assert any("recording failed" in r.message for r in caplog.records)


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


def test_clear_notes_is_project_write_even_when_connected(monkeypatch, tmp_path, no_reload):
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: [])
    spool = FakeSpool()
    session, store = make_session(
        control=FakeControl(hello=HELLO), spool=spool, project=tmp_path / "agent.flp"
    )

    receipt = session.clear_notes(TARGET)

    assert receipt.backend == "project-write" and receipt.confirmed_notes == 0
    assert spool.sent == []


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

    # the liveness ping precedes it (sticky-hello confirmation, A12)
    assert [c for c in control.calls if c[0] != "ping"] == [("state.snapshot", [])], (
        "exactly one composite round trip"
    )
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


def test_doctor_silent_wire_fix_names_the_dialog_not_a_restart(tmp_path, monkeypatch):
    """Ports self-heal now (hot-plug, docs/21 A12), so 'restart FL' is no longer
    the advice - the remaining silent-wire causes are a blocking dialog or a
    missing controller assignment."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(session_mod, "fl_process_running", lambda: True)
    session, _ = make_session(control=FakeControl(hello=None))

    report = session.doctor()

    assert report.control_plane.connected is False
    assert "dialog" in report.control_plane.fix.lower()
    assert "launch order does not matter" in report.control_plane.fix.lower()


# --- create_session ----------------------------------------------------------


def test_create_session_is_the_factory(monkeypatch):
    """Never real ports in the fast suite: a REAL MidiControl here made FL
    (when open) hot-plug onto test ports and segfault rtmidi's C layer at
    teardown - the live-found cause of the FL-open suite crashes."""
    fake = FakeControl(hello=HELLO)
    monkeypatch.setattr(session_mod, "MidiControl", lambda: fake)
    session = create_session()
    assert isinstance(session, FLSession)
    assert session.control is fake
    assert "/" not in session.store.project_key


def test_project_key_is_a_single_safe_component():
    """The raw project path as a key was a live bug: pathlib's join replaces
    the base on an absolute right-hand side, pointing the store at the .flp."""
    from fl_mcp.session import _project_key

    key = _project_key(Path("/Users/someone/Documents/FLMCP Agent.flp"))
    assert "/" not in key and "\\" not in key
    assert key.startswith("FLMCP Agent-")
    assert _project_key(None) == "default"
    # stable across calls, distinct across paths
    assert key == _project_key(Path("/Users/someone/Documents/FLMCP Agent.flp"))
    assert key != _project_key(Path("/elsewhere/FLMCP Agent.flp"))


def test_project_dir_rejects_path_separators():
    from fl_mcp import paths

    with pytest.raises(ValueError):
        paths.project_dir("/absolute/path.flp")


def test_silent_wire_recycles_ports_when_fl_is_running(monkeypatch):
    """Live-found (2026-08-29): FL binds ports that APPEAR while it runs (~2s)
    but can miss ports that existed at launch. A silent wire with FL up must
    recycle the ports and pick up the hot-plug hello."""
    silent = FakeControl(hello=None)
    silent.closed = False
    silent.close = lambda: setattr(silent, "closed", True)
    healed = FakeControl(hello=HELLO)
    session, _ = make_session(control=silent)
    session._control_factory = lambda: healed
    monkeypatch.setattr(session_mod, "fl_process_running", lambda: True)

    assert session._hello() is HELLO
    assert silent.closed
    assert session.control is healed


def test_silent_wire_does_not_recycle_when_fl_is_closed(monkeypatch):
    silent = FakeControl(hello=None)
    calls: list[bool] = []
    session, _ = make_session(control=silent)
    session._control_factory = lambda: calls.append(True)
    monkeypatch.setattr(session_mod, "fl_process_running", lambda: False)

    assert session._hello() is None
    assert calls == []


def test_recycle_is_rate_limited(monkeypatch):
    """An FL that is running but genuinely unconfigured must cost one recycle
    wait per interval, not one per tool call."""
    silent = FakeControl(hello=None)
    silent.close = lambda: None
    session, _ = make_session(control=silent)
    factory_calls: list[bool] = []

    def factory():
        factory_calls.append(True)
        still_silent = FakeControl(hello=None)
        still_silent.close = lambda: None
        return still_silent

    session._control_factory = factory
    monkeypatch.setattr(session_mod, "fl_process_running", lambda: True)

    assert session._hello() is None
    assert session._hello() is None  # immediately again - inside the interval
    assert factory_calls == [True]


def test_sticky_hello_is_not_trusted_without_a_live_ping(monkeypatch):
    """After FL dies, the wire still holds its last hello - trusting it routed
    live ops into a void (dogfood A12). A dead ping must read as disconnected."""

    class DeadWire(FakeControl):
        def send_request(self, op, args=None, kwargs=None, timeout=2.0):
            return ControlResponse(id=1, ok=False, err="no response")

    session, _ = make_session(control=DeadWire(hello=HELLO))
    monkeypatch.setattr(session_mod, "fl_process_running", lambda: False)

    assert session._hello() is None


def test_reload_reconnects_the_wire_fresh(monkeypatch, tmp_path):
    """Our own reload kills FL and the old ports; a fresh wire after the
    relaunch hits FL's reliable hot-plug bind and must replace the dead one."""
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    monkeypatch.setattr(session_mod, "fl_process_running", lambda: False)  # skip the pkill branch
    monkeypatch.setattr(session_mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())
    monkeypatch.setattr(session_mod, "SETTLE_TIMEOUT_SECONDS", 0.0)  # no FL to settle on
    old = FakeControl(hello=HELLO)
    fresh = FakeControl(hello=HELLO)
    session, _ = make_session(control=old, spool=FakeSpool(), project=tmp_path / "agent.flp")
    session._control_factory = lambda: fresh
    session._last_recycle = 12345.0

    session.write_notes(TARGET, NOTES, "merge")

    assert old.closed and session.control is fresh
    assert session._last_recycle == 0.0


# --- plugin parameters (live-only; E2-verified ops) --------------------------


def _param_control(values: dict[int, float] | None = None) -> FakeControl:
    """A generator with three params, mirroring the real wire ops."""
    state = values if values is not None else {0: 0.8, 1: 1.0, 2: 0.75}
    names = {0: "Main - Master level", 1: "Main - Master LFO level", 2: "Main - Master pitch"}
    quantum = 1 / 64  # FL quantizes to the control's grid (E2: 0.42 -> 0.41875)

    def set_value(args):
        value, index, _ch, _slot = args
        state[index] = round(value / quantum) * quantum
        return None

    return FakeControl(
        hello=HELLO,
        handlers={
            "plugins.getParamCount": lambda args: len(state),
            "plugins.getParamName": lambda args: names[args[0]],
            "plugins.getParamValue": lambda args: state[args[0]],
            "plugins.setParamValue": set_value,
        },
    )


def test_plugin_params_lists_and_filters():
    session, _ = make_session(control=_param_control())

    all_params = session.plugin_params(0)
    assert [(p.index, p.name) for p in all_params] == [
        (0, "Main - Master level"),
        (1, "Main - Master LFO level"),
        (2, "Main - Master pitch"),
    ]
    lfo = session.plugin_params(0, query="lfo")
    assert [p.index for p in lfo] == [1]
    assert session.plugin_params(0, limit=2) == all_params[:2]


def test_set_plugin_param_returns_quantized_readback():
    session, _ = make_session(control=_param_control())

    confirmed = session.set_plugin_param(0, 0, 0.42)
    assert confirmed.name == "Main - Master level"
    assert confirmed.value == pytest.approx(0.421875)  # the grid, not the echo


def test_set_plugin_param_resolves_unique_name_and_rejects_ambiguity():
    session, _ = make_session(control=_param_control())

    confirmed = session.set_plugin_param(0, "master pitch", 0.5)
    assert confirmed.index == 2

    with pytest.raises(errors.ToolError, match="ambiguous"):
        session.set_plugin_param(0, "Master", 0.5)
    with pytest.raises(errors.ToolError, match="unknown"):
        session.set_plugin_param(0, "cutoff", 0.5)


def test_plugin_params_without_a_wire_is_an_honest_error():
    """Plugin state has no file representation - live-or-error, no fallback."""
    session, _ = make_session(control=None)
    with pytest.raises(errors.ToolError) as excinfo:
        session.plugin_params(0)
    assert excinfo.value.category == "control_plane_unavailable"


# --- settle after reload (issue #47 / experiment E7) ---------------------------


def test_settle_returns_once_snapshot_answers(monkeypatch):
    answers = iter([False, False, True])
    control = FakeControl(
        handlers={"state.snapshot": lambda args: ControlResponse(
            id=1, ok=next(answers), err=None
        )},
    )
    session, _ = make_session(control=control)
    monkeypatch.setattr(session_mod, "SETTLE_POLL_SECONDS", 0.0)
    session._settle()
    snapshot_calls = [c for c in control.calls if c[0] == "state.snapshot"]
    assert len(snapshot_calls) == 3


def test_settle_times_out_quietly_when_fl_never_answers(monkeypatch, caplog):
    control = FakeControl(handlers={})  # snapshot always errors
    session, _ = make_session(control=control)
    monkeypatch.setattr(session_mod, "SETTLE_POLL_SECONDS", 0.0)
    monkeypatch.setattr(session_mod, "SETTLE_TIMEOUT_SECONDS", 0.05)
    session._settle()  # must not raise: the reload itself succeeded
    assert any("settle" in r.message for r in caplog.records)
