"""server.py tool tests against an injected fake session (G2 fast half).

The tools are exercised directly with a stub Context carrying the same
lifespan-context shape the real lifespan yields; no MIDI, no FL. The live
hello/tempo round trip is the manager-run half of the gate.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult
from session_fakes import RENDER_SPAN

from fl_mcp import __version__, server
from fl_mcp.audio import AudioAnalysis, SpectralBalance
from fl_mcp.contract import build_version
from fl_mcp.errors import ToolError
from fl_mcp.kernel.host import RUN_CELL_TIMEOUT_SECONDS, KernelHost
from fl_mcp.kernel.launch import PosixLauncher, Workspace
from fl_mcp.platform import MacPlatform
from fl_mcp.schemas import DoctorReport, Note, NoteSetRef, PlaneStatus, RenderReceipt, Target


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
        self.platform = MacPlatform()
        self.audio_stream_started = False
        self.closed = False

    def start_audio_stream_lifecycle(self) -> None:
        self.audio_stream_started = True

    def close(self) -> None:
        self.closed = True

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


def _ctx(fl: Any = None, session: Any = None, kernel: Any = None) -> Any:
    """A stub Context: same lifespan-context shape the real lifespan yields."""
    session = _FakeSession() if session is None else session
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"fl": fl, "session": session, "kernel": kernel})
    )


@pytest.fixture
def kernel(fake_session, tmp_path):
    """A real kernel process over the sdk_fakes session: run is exercised end to end."""
    host = KernelHost(fake_session, Workspace.create(tmp_path / "kernel"), PosixLauncher(), os.environ)
    yield host
    host.close()


# -- run ----------------------------------------------------------------------


def test_run_returns_repr_of_last_expression(kernel):
    result = server.run("x = 2\nx * 3", _ctx(kernel=kernel))
    assert result.outcome == "ok"
    assert result.stdout == ""
    assert result.result == "6"


def test_run_captures_stdout_without_leaking_to_the_wire(kernel, capsys):
    result = server.run("print('hi')", _ctx(kernel=kernel))
    assert result.stdout == "hi\n"
    assert result.result is None
    assert capsys.readouterr().out == ""  # nothing reached the server's stdout, the JSON-RPC wire


def test_run_namespace_holds_fl_and_theory(kernel):
    assert server.run("fl.tempo", _ctx(kernel=kernel)).result == "120.0"
    assert server.run("theory.chord('C4')", _ctx(kernel=kernel)).result == "[60, 64, 67]"


def test_run_error_reads_like_python_with_no_engine_frames(kernel):
    result = server.run("print('before')\n1 / 0", _ctx(kernel=kernel))
    assert result.outcome == "error"
    assert result.stdout == "before\n"  # output up to the failure is kept
    tb = result.traceback
    assert "ZeroDivisionError" in tb
    assert '"<fl>", line 2' in tb
    assert "cell.py" not in tb  # the exec plumbing is not the user's problem
    assert result.result is None


def test_run_syntax_error_is_a_traceback_not_a_crash(kernel):
    result = server.run("def broken(:", _ctx(kernel=kernel))
    assert result.outcome == "error"
    assert "SyntaxError" in result.traceback


def test_run_is_full_python(kernel):
    """The owner's call (2026-10-18): every import, introspection, and builtin.

    The OS, not a Python allowlist, is the boundary (docs/38).
    """
    code = "import inspect, os, socket\ntype(fl).__name__, callable(getattr(fl, 'track')), 'track' in dir(fl)"
    result = server.run(code, _ctx(kernel=kernel))
    assert result.result == "('FL', True, True)", result


def test_run_docstring_states_the_real_cell_budget():
    assert f"{RUN_CELL_TIMEOUT_SECONDS:g} s budget" in server.run.__doc__


def test_run_without_a_kernel_is_a_structured_error(tmp_path):
    """A kernel that cannot start reaches the model as kernel_unavailable, not a crash."""

    class _NoLaunch:
        confined = False

        def __call__(self, argv, *, workspace, env):
            raise OSError("no interpreter")

    host = KernelHost(_FakeSession(), Workspace.create(tmp_path), _NoLaunch(), {})
    response = server.run("1", _ctx(kernel=host))

    assert isinstance(response, CallToolResult)
    payload = json.loads(response.content[0].text)
    assert payload["error"] == "kernel_unavailable"
    assert "no interpreter" in payload["message"]


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
    assert result.target == target
    assert result.provenance == "project"
    assert result.as_of == "2026-08-27T00:00:00+00:00"
    assert result.note_count == 3
    assert len(result.notes) == 3
    assert result.notes[0].key == 60
    assert session.read_targets == [target]


def test_get_notes_large_returns_a_summary_not_a_page_dump():
    session = _FakeSession(notes=_notes(server.GET_NOTES_FULL_LIST_MAX + 100))
    result = server.get_notes(1, 0, _ctx(session=session))
    assert result.notes is None
    summary = result.summary
    assert result.note_count == server.GET_NOTES_FULL_LIST_MAX + 100
    assert summary.keys == {"min": 60, "max": 71}
    assert summary.span_beats["start"] == 0.0
    assert len(summary.sample) == server.GET_NOTES_SAMPLE_SIZE


def test_get_notes_tolerates_numeric_string_channel():
    session = _FakeSession(notes=[])
    result = server.get_notes(1, "3", _ctx(session=session))
    assert result.target == Target(pattern=1, channel=3)
    assert result.provenance is None


def test_get_notes_rejects_a_bad_target_with_a_fix():
    response = server.get_notes(0, 0, _ctx(session=_FakeSession()))

    assert isinstance(response, CallToolResult)
    assert response.is_error
    result = json.loads(response.content[0].text)
    assert result["error"] == "invalid_argument"
    assert result["fix"]


def test_get_notes_surfaces_tool_errors_structured():
    err = ToolError("note_plane_unavailable", "no backend", "run fl_doctor", retryable=True)
    session = _FakeSession(error=err)
    response = server.get_notes(1, 0, _ctx(session=session))

    assert isinstance(response, CallToolResult)
    assert response.is_error
    result = json.loads(response.content[0].text)
    assert result["status"] == "error"
    assert result["error"] == "note_plane_unavailable"


# -- fl_doctor ----------------------------------------------------------------


def test_fl_doctor_returns_the_typed_report():
    session = _FakeSession()
    result = server.fl_doctor(_ctx(session=session))
    assert result == session.doctor()
    assert result.control_plane.fix == "open FL"


def test_lifespan_builds_the_kernel_workspace_under_the_box_root(monkeypatch, tmp_path):
    session = _FakeSession()
    monkeypatch.setattr(server, "create_session", lambda: session)
    monkeypatch.setattr(server.sdk, "connect", lambda _: object())
    monkeypatch.setenv(server.ENV_KERNEL_ROOT, str(tmp_path))
    seen = {}

    async def start_and_stop() -> None:
        async with server.lifespan(None) as context:
            seen["workspace"] = context["kernel"].workspace

    anyio.run(start_and_stop)

    assert seen["workspace"].root == tmp_path.resolve()
    assert seen["workspace"].work.is_dir() and seen["workspace"].renders.is_dir()
    assert session.audio_stream_started
    assert session.closed


def test_lifespan_without_a_box_root_removes_its_temp_workspace(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(server, "create_session", lambda: session)
    monkeypatch.setattr(server.sdk, "connect", lambda _: object())
    monkeypatch.delenv(server.ENV_KERNEL_ROOT, raising=False)
    seen = {}

    async def start_and_stop() -> None:
        async with server.lifespan(None) as context:
            seen["root"] = context["kernel"].workspace.root
            assert seen["root"].is_dir()

    anyio.run(start_and_stop)
    assert not seen["root"].exists()


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


def test_server_info_uses_the_installed_package_version_and_build_description():
    mcp = server.create_server()

    assert mcp.name == server.SERVER_NAME
    assert mcp.version == __version__
    assert build_version() in mcp.description


# --- run() namespace (addendum A12) -------------------------------------------


def test_run_namespace_can_actually_write_notes(kernel, fake_session):
    """The regression this guards: ``Note`` was missing from run()'s namespace,
    which made the product's PRIMARY operation inexpressible. The write now
    crosses the kernel wire and lands in the server's session store.
    """
    out = server.run(
        "notes = [Note(key='C4', start=0.0, length=1.0)]\nfl.track('Bass').write(notes, mode='replace').confirmed_notes",
        _ctx(kernel=kernel),
    )

    assert out.traceback is None, out.traceback
    assert out.result == "1"
    assert [n.key for n in fake_session.read_notes(Target(pattern=1, channel=1))] == [60]


# --- MCP registration round trip (addendum A15) --------------------------------


def test_tools_register_with_ctx_injected_and_absent_from_the_schema():
    """The regression this guards: Context injection is ANNOTATION-based
    (`find_context_parameter` matches the Context class). With the skeleton's
    `ctx: Any`, ctx is not recognised, so it is NOT skipped from the input
    schema - it becomes a required tool argument no caller can supply, and the
    lifespan value never arrives. Verified against the installed mcp package.
    """
    from mcp.server.mcpserver import MCPServer

    srv = MCPServer(name="fl-mcp-test")
    for fn in (server.run, server.get_notes, server.fl_doctor):
        srv.add_tool(fn)

    tools = {t.name: t for t in srv._tool_manager.list_tools()}
    assert set(tools) == {"run", "get_notes", "fl_doctor"}
    for name, tool in tools.items():
        assert tool.context_kwarg == "ctx", f"{name}: ctx not detected for injection"
        assert "ctx" not in tool.parameters.get("properties", {}), (
            f"{name}: ctx leaked into the input schema as a caller-supplied argument"
        )
    # The tools that take real arguments still expose them.
    assert "code" in tools["run"].parameters["properties"]
    assert {"pattern", "channel"} <= set(tools["get_notes"].parameters["properties"])
    assert tools["fl_doctor"].parameters.get("properties", {}) == {}


# -- render / analyze -----------------------------------------------------------


class _RenderFl:
    """SDK stub: render writes a tiny WAV; analyze measures a given path."""

    def __init__(self) -> None:
        self.analyzed: list[Path] = []

    def render(self, output: Path) -> Path:
        return self.render_receipt(output).path

    def render_receipt(self, output: Path) -> RenderReceipt:
        import wave

        import numpy as np

        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes((np.zeros(441)).astype("<i2").tobytes())
        return RenderReceipt(path=output, span=RENDER_SPAN)

    def analyze(self, wav: Path) -> AudioAnalysis:
        self.analyzed.append(Path(wav))
        return AudioAnalysis(
            bpm=140.0,
            key=None,
            integrated_lufs=-12.0,
            true_peak_dbfs=-1.0,
            spectral_balance=SpectralBalance(low_dbfs=-20.0, mid_dbfs=-20.0, high_dbfs=-20.0),
        )


def _workspace_only(tmp_path: Path) -> Any:
    """The render tools read only the kernel host's workspace."""
    return SimpleNamespace(workspace=Workspace.create(tmp_path / "kernel"))


def test_render_returns_path_wav_base64_and_live_span(tmp_path):
    import base64

    kernel = _workspace_only(tmp_path)
    result = server.render(_ctx(fl=_RenderFl(), kernel=kernel))
    assert Path(result.path).exists()
    # In the renders folder, so model code in run can read the same WAV.
    assert Path(result.path).is_relative_to(kernel.workspace.renders)
    assert base64.b64decode(result.wavBase64)[:4] == b"RIFF"
    assert result.span == RENDER_SPAN  # the caller sees what played and for how long


def test_analyze_defaults_to_the_most_recent_render(tmp_path):
    fl = _RenderFl()
    ctx = _ctx(fl=fl, kernel=_workspace_only(tmp_path))
    rendered = server.render(ctx)
    metrics = server.analyze(ctx)
    assert metrics.bpm == 140.0
    assert metrics.integrated_lufs == -12.0
    assert fl.analyzed == [Path(rendered.path)]


def test_analyze_renders_fresh_when_nothing_was_rendered_yet(tmp_path):
    fl = _RenderFl()
    metrics = server.analyze(_ctx(fl=fl, kernel=_workspace_only(tmp_path)))
    assert metrics.bpm == 140.0
    assert len(fl.analyzed) == 1
