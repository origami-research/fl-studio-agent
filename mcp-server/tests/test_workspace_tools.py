"""The workspace tools at the MCP boundary: the gate, the modes, the schemas, the words.

Hosted runs that pin a wheel must see exactly the FL tools they were built for, so the
workspace tools appear only behind FLMCP_WORKSPACE_TOOLS=1 or under
--workspace-only. Results go through the real MCP layer here, because the
harness cuts a result at 20,000 characters of what that layer prints.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from mcp.types import CallToolResult

from fl_mcp import actions, server
from fl_mcp.contract import build_contract, server_description, workspace_tools_from_env
from fl_mcp.kernel.launch import PosixLauncher, Workspace
from fl_mcp.workspace import ops
from fl_mcp.workspace.runner import OpTimeout, WorkspaceRunner

FL_TOOLS = ["run", "get_scene", "check", "render", "analyze"]
WORKSPACE_TOOLS = ["shell", "read_file", "write_file", "edit_file", "glob", "grep"]
HARNESS_RESULT_CHARS = 20_000
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")


class _PosixPlatform:
    def kernel_launcher(self) -> PosixLauncher:
        return PosixLauncher()


@pytest.fixture
def runner(tmp_path):
    runner = WorkspaceRunner(Workspace.create(tmp_path / "root"), PosixLauncher(), os.environ)
    yield runner
    runner.close()


def _ctx(runner: WorkspaceRunner) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context={"workspace": runner}))


def _error(response: Any) -> dict:
    assert isinstance(response, CallToolResult) and response.is_error, response
    return json.loads(response.content[0].text)


# -- the gate and the modes ---------------------------------------------------------------


def _listed(**modes: bool) -> list[str]:
    return [tool.name for tool in anyio.run(server.create_server(**modes).list_tools)]


def test_the_default_surface_is_the_fl_tools_alone():
    """Pinning a new wheel must not change what fl-mcp-v1 models are offered."""
    assert _listed() == FL_TOOLS


def test_the_gate_adds_the_workspace_tools_after_the_fl_tools():
    assert _listed(workspace_tools=True) == FL_TOOLS + WORKSPACE_TOOLS


def test_workspace_only_serves_the_workspace_tools_alone():
    assert _listed(workspace_only=True) == WORKSPACE_TOOLS


def test_the_gate_reads_only_0_or_1():
    assert workspace_tools_from_env({}) is False
    assert workspace_tools_from_env({"FLMCP_WORKSPACE_TOOLS": "0"}) is False
    assert workspace_tools_from_env({"FLMCP_WORKSPACE_TOOLS": "1"}) is True
    with pytest.raises(ValueError, match="must be 0 or 1"):
        workspace_tools_from_env({"FLMCP_WORKSPACE_TOOLS": "yes"})


@pytest.mark.parametrize(
    ("argv", "gate", "expected"),
    [
        ([], None, {"workspace_only": False, "workspace_tools": False}),
        ([], "1", {"workspace_only": False, "workspace_tools": True}),
        (["--workspace-only"], None, {"workspace_only": True, "workspace_tools": False}),
    ],
)
def test_main_takes_the_mode_from_its_flag_and_the_gate_from_the_environment(
    monkeypatch, argv, gate, expected
):
    seen = {}
    monkeypatch.setattr(
        server, "create_server", lambda **modes: seen.update(modes) or SimpleNamespace(run=lambda: None)
    )
    monkeypatch.setattr(server.logging, "basicConfig", lambda **_: None)
    if gate is None:
        monkeypatch.delenv("FLMCP_WORKSPACE_TOOLS", raising=False)
    else:
        monkeypatch.setenv("FLMCP_WORKSPACE_TOOLS", gate)
    server.main(argv)
    assert seen == expected


def test_server_info_says_whether_the_host_confines_model_code(monkeypatch):
    unconfined = SimpleNamespace(kernel_launcher=lambda: SimpleNamespace(confined=False))
    monkeypatch.setattr(server, "platform_for", lambda _: unconfined)
    assert server.create_server().description.endswith("; confined=false")
    confined = SimpleNamespace(kernel_launcher=lambda: SimpleNamespace(confined=True))
    monkeypatch.setattr(server, "platform_for", lambda _: confined)
    assert server.create_server().description.endswith("; confined=true")
    assert server_description(True) == server.create_server().description


def test_workspace_only_builds_no_fl_session_and_releases_its_workspace(monkeypatch, tmp_path):
    released = []

    class _Launcher(PosixLauncher):
        def release(self, workspace):
            released.append(workspace.root)

    monkeypatch.setattr(server, "create_session", lambda: pytest.fail("no FL session in --workspace-only"))
    monkeypatch.setattr(server, "platform_for", lambda _: SimpleNamespace(kernel_launcher=_Launcher))
    monkeypatch.setenv(server.ENV_KERNEL_ROOT, str(tmp_path))
    seen = {}

    async def start_and_stop() -> None:
        async with server.workspace_lifespan(None) as context:
            seen.update(context)

    anyio.run(start_and_stop)
    assert set(seen) == {"workspace"}
    assert seen["workspace"].workspace.root == tmp_path.resolve()
    assert released == [tmp_path.resolve()]


def test_the_gated_lifespan_shares_one_launcher_and_workspace_with_the_kernel(monkeypatch, tmp_path):
    from test_server import _FakeSession

    session = _FakeSession()
    monkeypatch.setattr(server, "create_session", lambda: session)
    monkeypatch.setattr(server.sdk, "connect", lambda _: object())
    monkeypatch.setenv(server.ENV_KERNEL_ROOT, str(tmp_path))
    seen = {}

    async def start_and_stop() -> None:
        async with server.lifespan(None, workspace_tools=True) as context:
            seen.update(context)

    anyio.run(start_and_stop)
    assert seen["workspace"].workspace == seen["kernel"].workspace
    assert seen["workspace"]._launcher is seen["kernel"]._launcher
    assert session.closed


def test_a_failing_workspace_close_still_closes_the_kernel_and_the_session(monkeypatch, tmp_path):
    """Each close is its own step: a revoke icacls refuses must not leave FL or the kernel running."""
    from test_server import _FakeSession

    session = _FakeSession()
    monkeypatch.setattr(server, "create_session", lambda: session)
    monkeypatch.setattr(server.sdk, "connect", lambda _: object())
    monkeypatch.setenv(server.ENV_KERNEL_ROOT, str(tmp_path))
    closed = []

    def refuse(self):
        raise OSError("icacls")

    monkeypatch.setattr(WorkspaceRunner, "close", refuse)
    monkeypatch.setattr(server.KernelHost, "close", lambda self: closed.append("kernel"))

    async def start_and_stop() -> None:
        async with server.lifespan(None, workspace_tools=True):
            pass

    with pytest.raises(OSError, match="icacls"):
        anyio.run(start_and_stop)
    assert closed == ["kernel"] and session.closed


# -- the tools, end to end over a real op process ----------------------------------------------


@posix_only
def test_a_session_of_workspace_calls_reads_like_a_coding_agent(runner):
    ctx = _ctx(runner)
    wrote = server.write_file("notes/plan.md", "groove: TODO\n", ctx)
    assert wrote.created and wrote.actions == ["Wrote notes/plan.md (13 bytes)"]
    edited = server.edit_file("notes/plan.md", "TODO", "four on the floor", ctx)
    assert edited.replacements == 1 and edited.actions == ["Edited notes/plan.md (1 change)"]
    read = server.read_file("notes/plan.md", ctx)
    assert read.content == "     1\tgroove: four on the floor" and read.actions == []
    assert server.glob("**/*.md", ctx).paths == ["notes/plan.md"]
    assert server.grep("floor", ctx, output_mode="lines").matches == [
        "notes/plan.md:1: groove: four on the floor"
    ]
    ran = server.shell("cat notes/plan.md; exit 1", ctx)
    assert ran.exit_code == 1 and ran.stdout == "groove: four on the floor\n"
    assert ran.actions == [f'Ran "cat notes/plan.md; exit 1" (exit 1, {ran.duration_s:.1f} s)']


@posix_only
def test_a_failing_command_is_a_result_not_an_error(runner):
    result = server.shell("false", _ctx(runner))
    assert not isinstance(result, CallToolResult) and result.exit_code == 1


def test_refusals_reach_the_model_structured_with_a_fix(runner):
    payload = _error(server.edit_file("missing.md", "a", "b", _ctx(runner)))
    assert payload["error"] == "invalid_argument" and "does not exist" in payload["message"]
    assert payload["fix"]


@pytest.mark.parametrize(
    ("call", "says"),
    [
        (lambda ctx: server.shell("", ctx), "16 KB"),
        (lambda ctx: server.shell("x" * (16 * 1024 + 1), ctx), "16 KB"),
        (lambda ctx: server.shell("ls", ctx, timeout_s=0), "1 to 600"),
        (lambda ctx: server.shell("ls", ctx, timeout_s=601), "1 to 600"),
        (lambda ctx: server.read_file("a", ctx, offset=0), "from 1"),
        (lambda ctx: server.read_file("a", ctx, limit=2001), "1 to 2000"),
        (lambda ctx: server.write_file("a", "x" * (1024 * 1024 + 1), ctx), "1 MB"),
        (lambda ctx: server.edit_file("a", "x" * (256 * 1024 + 1), "y", ctx), "256 KB"),
        (lambda ctx: server.read_file("a" * 4097, ctx), "4096 characters"),
        (lambda ctx: server.grep("x" * 4097, ctx), "4096 characters"),
        (lambda ctx: server.glob("*", ctx, path="p" * 4097), "4096 characters"),
    ],
)
def test_arguments_past_their_limits_are_refused_before_anything_runs(call, says):
    """A ctx with no runner proves nothing was launched."""
    payload = _error(call(_ctx(None)))
    assert payload["error"] == "invalid_argument" and says in payload["message"]


def test_a_shell_call_whose_op_never_answers_still_returns_a_timed_out_result(monkeypatch, runner):
    def hang(op, args, *, timeout):
        raise OpTimeout(op)

    monkeypatch.setattr(runner, "call", hang)
    result = server.shell("sleep 999", _ctx(runner), timeout_s=5)
    assert result.timed_out and result.exit_code is None and "output was lost" in result.note
    assert result.actions == ['Ran "sleep 999" (timed out after 5 s)']


def test_a_file_op_past_its_deadline_is_a_plain_refusal(monkeypatch, runner):
    def hang(op, args, *, timeout):
        raise OpTimeout(op)

    monkeypatch.setattr(runner, "call", hang)
    payload = _error(server.grep("(a+)+$", _ctx(runner)))
    assert payload["error"] == "invalid_argument" and "60 s" in payload["message"]


# -- what the harness receives fits its cap ------------------------------------------------------


def _call_over_mcp(monkeypatch, tmp_path, name: str, arguments: dict) -> Any:
    monkeypatch.setattr(server, "platform_for", lambda _: _PosixPlatform())
    monkeypatch.setenv(server.ENV_KERNEL_ROOT, str(tmp_path))
    work = Workspace.create(tmp_path).work
    (work / "wide.txt").write_text(('"\t' * 60 + "\n") * 2000)
    for n in range(600):
        (work / "many" / f"{n:04d}").mkdir(parents=True, exist_ok=True)
        (work / "many" / f"{n:04d}" / f"{'x' * 40}.txt").write_text("needle\n" * 3)

    async def call() -> Any:
        mcp = server.create_server(workspace_only=True)
        async with InMemoryTransport(mcp) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            return await client.call_tool(name, arguments)

    return anyio.run(call)


@posix_only
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("shell", {"command": "seq 1 200000; seq 1 50000 >&2"}),
        ("read_file", {"path": "wide.txt"}),
        ("glob", {"pattern": "**/*.txt"}),
        ("grep", {"pattern": "needle", "output_mode": "lines"}),
        ("grep", {"pattern": "needle", "output_mode": "count"}),
    ],
)
def test_the_largest_results_fit_what_the_harness_keeps(monkeypatch, tmp_path, name, arguments):
    result = _call_over_mcp(monkeypatch, tmp_path, name, arguments)
    assert not result.is_error, result.content[0].text
    assert len(result.content[0].text) <= HARNESS_RESULT_CHARS
    assert result.structured_content["truncated"] is True


def test_workspace_output_schemas_are_objects_through_the_mcp_layer(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_runner(_server):
        yield {}

    monkeypatch.setattr(server, "workspace_lifespan", no_runner)

    async def call() -> Any:
        async with (
            InMemoryTransport(server.create_server(workspace_only=True)) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            return await client.list_tools()

    for tool in anyio.run(call).tools:
        schema = tool.model_dump(by_alias=True)["outputSchema"]
        assert schema["type"] == "object" and "actions" in schema["properties"], tool.name
        assert "ctx" not in tool.input_schema.get("properties", {}), tool.name


def test_the_contract_publishes_the_workspace_tools_beside_the_fl_tools():
    contract = build_contract()
    assert [tool["name"] for tool in contract["tools"]] == FL_TOOLS
    assert [tool["name"] for tool in contract["workspace_tools"]] == WORKSPACE_TOOLS
    assert contract["env"]["FLMCP_WORKSPACE_TOOLS"] == ["0", "1"]
    assert contract["server"]["workspace_only_flag"] == "--workspace-only"


# -- the descriptions state the real limits -----------------------------------------------------


def _doc(fn: Any) -> str:
    return " ".join(fn.__doc__.split())


def test_descriptions_state_the_limits_the_code_enforces():
    assert ops.SHELL_TIMEOUT_DEFAULT == 120 and ops.SHELL_TIMEOUT_MAX == 600
    assert "1 to 600 (default 120)" in _doc(server.shell)
    assert ops.SHELL_COMMAND_BYTES == 16 * 1024 and "up to 16 KB" in _doc(server.shell)
    assert f"{ops.SHELL_OUTPUT_CHARS:,} characters" in _doc(server.shell)
    assert ops.READ_LINES == 2000 and "1 to 2000, default 2000" in _doc(server.read_file)
    assert f"longer than {ops.READ_LINE_CHARS} characters" in _doc(server.read_file)
    assert f"{ops.READ_CHARS:,} characters" in _doc(server.read_file)
    assert ops.WRITE_BYTES == 1024 * 1024 and "up to 1 MB" in _doc(server.write_file)
    assert ops.EDIT_STRING_BYTES == 256 * 1024 and "up to 256 KB" in _doc(server.edit_file)
    assert f"At most {ops.GLOB_PATHS} paths" in _doc(server.glob)
    assert f"At most {ops.GREP_RESULTS} results" in _doc(server.grep)
    assert f"over {ops.GREP_FILE_BYTES >> 20} MB" in _doc(server.grep)
    for fn in (server.glob, server.grep):
        assert all(name in _doc(fn) for name in sorted(ops.PLUMBING)), fn.__name__


def test_the_shell_sentence_shows_the_first_line_clipped():
    assert actions.ran_command("pip install mido", 0, 3.14, 120) == 'Ran "pip install mido" (exit 0, 3.1 s)'
    long = actions.ran_command("echo " + "x" * 200, 0, 1.0, 120)
    assert long.startswith('Ran "echo xxx') and len(long.split('"')[1]) == actions.COMMAND_CHARS
    assert actions.ran_command("cd a\npython b.py", 2, 0.5, 120) == 'Ran "cd a..." (exit 2, 0.5 s)'
    assert actions.ran_command("sleep 9", None, 9.0, 5) == 'Ran "sleep 9" (timed out after 5 s)'


def test_file_sizes_read_plainly():
    assert actions.file_size(1) == "1 byte" and actions.file_size(512) == "512 bytes"
    assert actions.file_size(1229) == "1.2 KB" and actions.file_size(3 << 20) == "3.0 MB"


def test_paths_in_sentences_are_what_the_model_passed_or_the_workspace_relative_path(runner):
    ctx = _ctx(runner)
    absolute = str(Path(runner.workspace.work) / "a" / "b.txt")
    assert server.write_file(absolute, "x", ctx).actions == ["Wrote a/b.txt (1 byte)"]
