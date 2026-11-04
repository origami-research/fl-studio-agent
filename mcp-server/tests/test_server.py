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
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult

from fl_mcp import __version__, server
from fl_mcp.contract import build_version
from fl_mcp.kernel.host import RUN_CELL_TIMEOUT_SECONDS, KernelHost
from fl_mcp.kernel.launch import PosixLauncher, Workspace
from fl_mcp.platform import MacPlatform
from fl_mcp.schemas import Target


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


def test_run_shows_the_images_a_cell_draws_as_image_content(kernel):
    """The model sees what its code drew, like a notebook: the image crosses from the
    kernel process and follows the result as MCP image content, never base64 text."""
    result = server.run("from PIL import Image\nImage.new('RGB', (320, 200), 'navy')", _ctx(kernel=kernel))

    assert isinstance(result, CallToolResult)
    text, image = result.content
    assert (image.type, image.mime_type) == ("image", "image/png")
    assert json.loads(text.text)["images"] == ["image 1: 320x200 PNG, about 86 tokens"]
    assert image.data not in text.text
    assert json.loads(text.text) == result.structured_content


def test_run_draws_matplotlib_into_the_result_never_a_window(kernel):
    """A plt.show() in a cell must not open a window on the machine running FL: the kernel
    draws it into the result."""
    pytest.importorskip("matplotlib")
    result = server.run("import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.show()", _ctx(kernel=kernel))

    assert isinstance(result, CallToolResult), result
    assert result.structured_content["images"] == ["image 1: 640x480 PNG, about 410 tokens"]


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


def test_the_model_sees_one_tool():
    """Owner, 2026-11-03: one tool, run. Reading the song, the meters, renders,
    listening, files and shell are all Python on fl inside it; setup diagnostics
    stay the operator's fl-mcp-doctor."""
    assert [fn.__name__ for fn in server.tool_definitions()] == ["run"]
    for word in ("fl.meters", "fl.render", "fl.mixer", "fl.playlist", "fl.patterns", "fl.read_wav", "librosa"):
        assert word in server.run.__doc__, word


def test_lifespan_builds_the_kernel_workspace_under_the_box_root(monkeypatch, tmp_path):
    session = _FakeSession()
    monkeypatch.setattr(server, "create_session", lambda: session)
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


def test_workspace_only_starts_no_fl_session_and_its_kernel_refuses_fl(monkeypatch, tmp_path):
    """--workspace-only (computer-use runs): run is plain Python; no FL session is
    made, so fl calls are refused instead of reaching for FL."""
    monkeypatch.setattr(server, "create_session", lambda: pytest.fail("an FL session was started"))
    monkeypatch.setattr(server, "platform_for", lambda _platform: MacPlatform())  # CI runs on Linux
    monkeypatch.setenv(server.ENV_KERNEL_ROOT, str(tmp_path))
    seen = {}

    async def start_and_stop() -> None:
        async with server.lifespan(None, with_fl=False) as context:
            seen["kernel"] = context["kernel"]
            seen["session"] = context["kernel"]._session

    anyio.run(start_and_stop)
    assert seen["session"] is None


def test_lifespan_without_a_box_root_removes_its_temp_workspace(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(server, "create_session", lambda: session)
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
        server.main([])
        assert ran == {"ok": True}
        assert root.level == logging.DEBUG
        streams = [h.stream for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert streams == [sys.stderr]
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_version_flag_answers_without_starting_the_server(monkeypatch, capsys):
    """A person checking an install runs `fl-mcp --version`; it must print and
    exit, not start a stdio server that waits on the terminal forever."""
    monkeypatch.setattr(server.MCPServer, "run", lambda self: pytest.fail("the server started"))
    with pytest.raises(SystemExit) as exited:
        server.main(["--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f"fl-studio-agent {server.__version__}"


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
    assert set(tools) == {"run"}
    for name, tool in tools.items():
        assert tool.context_kwarg == "ctx", f"{name}: ctx not detected for injection"
        assert "ctx" not in tool.parameters.get("properties", {}), (
            f"{name}: ctx leaked into the input schema as a caller-supplied argument"
        )
    assert "code" in tools["run"].parameters["properties"]


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
