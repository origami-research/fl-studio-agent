"""v2/server.py tool tests against an injected fake session (G2 fast half).

The tools are exercised directly with a stub Context carrying the same
lifespan-context shape the real lifespan yields; no MIDI, no FL. The live
hello/tempo round trip is the manager-run half of the gate.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fl_mcp.errors import ToolError
from fl_mcp.v2 import server
from fl_mcp.v2.schemas import DoctorReport, Note, NoteSetRef, PlaneStatus, Target


class _FakeStore:
    def __init__(self, ref: NoteSetRef | None):
        self._ref = ref

    def ref_at(self, target: Target) -> NoteSetRef | None:
        return self._ref


class _FakeSession:
    """Just the surface server.py touches: read_notes, store.ref_at, doctor."""

    def __init__(
        self,
        notes: list[Note] | None = None,
        ref: NoteSetRef | None = None,
        error: ToolError | None = None,
    ):
        self._notes = notes or []
        self._error = error
        self.store = _FakeStore(ref)
        self.read_targets: list[Target] = []
        self.control = None

    def read_notes(self, target: Target) -> list[Note]:
        if self._error is not None:
            raise self._error
        self.read_targets.append(target)
        return self._notes

    def doctor(self) -> DoctorReport:
        return DoctorReport(
            status="degraded",
            control_plane=PlaneStatus(connected=False, detail="no hello", fix="open FL"),
            note_plane=PlaneStatus(connected=True),
            project_file=PlaneStatus(connected=True),
        )


def _ctx(fl: Any = None, session: Any = None) -> Any:
    """A stub Context: same lifespan-context shape the real lifespan yields."""
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"fl": fl, "session": session})
    )


# -- run ----------------------------------------------------------------------


def test_run_returns_repr_of_last_expression():
    result = server.run("x = 2\nx * 3", _ctx())
    assert result == {"stdout": "", "result": "6"}


def test_run_captures_stdout_without_leaking_to_the_wire(capsys):
    result = server.run("print('hi')", _ctx())
    assert result == {"stdout": "hi\n", "result": None}
    assert capsys.readouterr().out == ""  # nothing reached real stdout


def test_run_namespace_holds_fl_and_theory():
    fl = SimpleNamespace(tempo=120.5)
    result = server.run("fl.tempo", _ctx(fl=fl))
    assert result["result"] == "120.5"
    result = server.run("theory.__name__", _ctx())
    assert result["result"] == "'fl_mcp.theory'"


def test_run_error_reads_like_python_with_no_server_frames():
    result = server.run("print('before')\n1 / 0", _ctx())
    assert result["stdout"] == "before\n"  # output up to the failure is kept
    tb = result["traceback"]
    assert "ZeroDivisionError" in tb
    assert '"<fl>", line 2' in tb
    assert "server.py" not in tb  # the exec plumbing is not the user's problem
    assert "result" not in result


def test_run_syntax_error_is_a_traceback_not_a_crash():
    result = server.run("def broken(:", _ctx())
    assert "SyntaxError" in result["traceback"]


def test_run_survives_sys_exit():
    result = server.run("import sys\nsys.exit(3)", _ctx())
    assert "SystemExit" in result["traceback"]


def test_run_assignment_only_yields_none_result():
    result = server.run("x = 41", _ctx())
    assert result == {"stdout": "", "result": None}


# -- get_notes ----------------------------------------------------------------


def _notes(count: int) -> list[Note]:
    return [Note(key=60 + (i % 12), start=float(i), length=1.0) for i in range(count)]


def _ref(target: Target) -> NoteSetRef:
    return NoteSetRef(
        id="abc123",
        target=target,
        provenance="project",
        as_of=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )


def test_get_notes_small_returns_the_full_list_with_provenance():
    target = Target(pattern=2, channel=3)
    session = _FakeSession(notes=_notes(3), ref=_ref(target))
    result = server.get_notes(2, 3, _ctx(session=session))
    assert result["target"] == {"pattern": 2, "channel": 3}
    assert result["provenance"] == "project"
    assert result["as_of"] == "2026-08-27T00:00:00+00:00"
    assert result["note_count"] == 3
    assert len(result["notes"]) == 3
    assert result["notes"][0]["key"] == 60
    assert session.read_targets == [target]


def test_get_notes_large_returns_a_summary_not_a_page_dump():
    session = _FakeSession(notes=_notes(server.GET_NOTES_FULL_LIST_MAX + 100))
    result = server.get_notes(1, 0, _ctx(session=session))
    assert "notes" not in result
    summary = result["summary"]
    assert result["note_count"] == server.GET_NOTES_FULL_LIST_MAX + 100
    assert summary["keys"] == {"min": 60, "max": 71}
    assert summary["span_beats"]["start"] == 0.0
    assert len(summary["sample"]) == server.GET_NOTES_SAMPLE_SIZE


def test_get_notes_tolerates_numeric_string_channel():
    session = _FakeSession(notes=[])
    result = server.get_notes(1, "3", _ctx(session=session))
    assert result["target"] == {"pattern": 1, "channel": 3}
    assert result["provenance"] is None


def test_get_notes_rejects_a_bad_target_with_a_fix():
    result = server.get_notes(0, 0, _ctx(session=_FakeSession()))
    assert "error" in result
    assert "fix" in result


def test_get_notes_surfaces_tool_errors_structured():
    err = ToolError("note_plane_unavailable", "no backend", "run fl_doctor", retryable=True)
    session = _FakeSession(error=err)
    result = server.get_notes(1, 0, _ctx(session=session))
    assert result["status"] == "error"
    assert result["error"] == "note_plane_unavailable"


# -- fl_doctor ----------------------------------------------------------------


def test_fl_doctor_is_exactly_the_model_dump():
    session = _FakeSession()
    result = server.fl_doctor(_ctx(session=session))
    assert result == session.doctor().model_dump()
    assert result["control_plane"]["fix"] == "open FL"


# -- main / logging -----------------------------------------------------------


def test_main_logs_to_stderr_at_the_env_level(monkeypatch):
    """stdout is the JSON-RPC wire: main() must bind logging to stderr and
    honor $FL_MCP_LOG before serving."""
    ran = {}
    monkeypatch.setattr(server.MCPServer, "run", lambda self: ran.setdefault("ok", True))
    monkeypatch.setenv(server.LOG_LEVEL_ENV, "debug")

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        server.main()
        assert ran == {"ok": True}
        assert root.level == logging.DEBUG
        streams = [h.stream for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert streams == [sys.stderr]
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
