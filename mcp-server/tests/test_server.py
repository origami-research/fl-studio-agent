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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult
from session_fakes import RENDER_SPAN

from fl_mcp import __version__, errors, server
from fl_mcp.audio import AudioAnalysis, SpectralBalance
from fl_mcp.contract import build_version
from fl_mcp.kernel.host import RUN_CELL_TIMEOUT_SECONDS, KernelHost
from fl_mcp.kernel.launch import PosixLauncher, Workspace
from fl_mcp.platform import MacPlatform
from fl_mcp.schemas import CheckResult, RenderReceipt, Target


class _FakeSession:
    """Just the surface the lifespan and run touch."""

    def __init__(self) -> None:
        self.control = None
        self.platform = MacPlatform()
        self.audio_stream_started = False
        self.closed = False

    def start_audio_stream_lifecycle(self) -> None:
        self.audio_stream_started = True

    def close(self) -> None:
        self.closed = True


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


# -- the tool surface -----------------------------------------------------------


def test_the_model_sees_one_tool_per_job():
    """One way to do each thing (owner, 2026-10-18): read the song, run code,
    prove it sounds, hear it, measure it. Setup diagnostics are the operator's
    fl-mcp-doctor, and note reads live in get_scene and the SDK, so neither
    fl_doctor nor get_notes is offered to a model."""
    assert [fn.__name__ for fn in server.tool_definitions()] == ["run", "get_scene", "check", "render", "analyze"]


def test_check_returns_the_sessions_result():
    result = CheckResult(
        start_bar=3, bars=1, seconds=1.85, master_peak_dbfs=-3.0, channels=[], summary="0 of 0 channels sounded."
    )
    session = SimpleNamespace(check=lambda start_bar, bars: result if (start_bar, bars) == (3, 1) else None)
    checked = server.check(_ctx(session=session), bars=1, start_bar=3)
    assert CheckResult.model_validate(checked.model_dump(exclude={"actions"})) == result


def test_check_refusals_reach_the_model_structured():
    def refuse(start_bar: int, bars: int) -> CheckResult:
        raise errors.invalid_argument("check", "bars must be a whole number from 1 to 8, got 40")

    response = server.check(_ctx(session=SimpleNamespace(check=refuse)), bars=40)
    assert isinstance(response, CallToolResult)
    assert response.is_error
    assert json.loads(response.content[0].text)["error"] == "invalid_argument"


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
        "notes = [Note(key='C4', start=0.0, length=1.0)]\nfl.track('Bass').write(notes, mode='replace').total",
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
    for fn in server.tool_definitions():
        srv.add_tool(fn)

    tools = {t.name: t for t in srv._tool_manager.list_tools()}
    assert set(tools) == {"run", "get_scene", "check", "render", "analyze"}
    for name, tool in tools.items():
        assert tool.context_kwarg == "ctx", f"{name}: ctx not detected for injection"
        assert "ctx" not in tool.parameters.get("properties", {}), (
            f"{name}: ctx leaked into the input schema as a caller-supplied argument"
        )
    # The tools that take real arguments still expose them.
    assert "code" in tools["run"].parameters["properties"]
    assert {"bars", "start_bar"} == set(tools["check"].parameters["properties"])
    assert tools["render"].parameters.get("properties", {}) == {}


# -- render / analyze -----------------------------------------------------------


class _RenderFl:
    """SDK and session stub: render writes a tiny WAV; analyze measures a given path."""

    def __init__(self) -> None:
        self.analyzed: list[Path] = []

    def render(self, output: Path) -> Path:
        return self.render_wav(output).path

    def render_wav(self, output: Path) -> RenderReceipt:
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
    stub = _RenderFl()
    result = server.render(_ctx(fl=stub, session=stub, kernel=kernel))
    assert Path(result.path).exists()
    # In the renders folder, so model code in run can read the same WAV.
    assert Path(result.path).is_relative_to(kernel.workspace.renders)
    assert base64.b64decode(result.wavBase64)[:4] == b"RIFF"
    assert result.span == RENDER_SPAN  # the caller sees what played and for how long


def test_analyze_defaults_to_the_most_recent_render(tmp_path):
    fl = _RenderFl()
    ctx = _ctx(fl=fl, session=fl, kernel=_workspace_only(tmp_path))
    rendered = server.render(ctx)
    metrics = server.analyze(ctx)
    assert metrics.bpm == 140.0
    assert metrics.integrated_lufs == -12.0
    assert fl.analyzed == [Path(rendered.path)]


def test_analyze_renders_fresh_when_nothing_was_rendered_yet(tmp_path):
    fl = _RenderFl()
    metrics = server.analyze(_ctx(fl=fl, session=fl, kernel=_workspace_only(tmp_path)))
    assert metrics.bpm == 140.0
    assert len(fl.analyzed) == 1


def test_notes_take_positional_arguments_in_the_documented_order(kernel):
    """Every model in the 2026-10-19 live run first wrote Note(60, 0, 1, 0.8) and lost a step to
    a TypeError; position or name now both work, in the cheatsheet's order."""
    same = server.run(
        "Note(60, 0, 1, 0.8) == Note(key='C4', start=0, length=1, velocity=0.8)"
        " == Note('C4', 0, 1, velocity=0.8) == Note(60, 0, 1, 0.8, 0.5, 0)",
        _ctx(kernel=kernel),
    )
    assert same.result == "True"
    too_many = server.run("Note(60, 0, 1, 0.8, 0.5, 0, 1)", _ctx(kernel=kernel))
    assert "Note takes at most 6 positional arguments (key, start, length, velocity, pan, color)" in too_many.traceback
    twice = server.run("Note(60, 0, 1, key=61)", _ctx(kernel=kernel))
    assert "Note got key both by position and by name" in twice.traceback
    invalid = server.run("Note(200, 0, 1)", _ctx(kernel=kernel))
    assert invalid.outcome == "error" and "key" in invalid.traceback
