"""The workspace runner: one launched op process per call, its tree stopped after.

The POSIX half drives real processes through PosixLauncher: timeouts, the
process tree, the built environment, and every way an op can fail to answer.
The Windows half drives AppContainerLauncher over the recorded Win32 fake: the
container, the smaller tool job, the probe before the first call, and the job
terminated on a timeout. The real AppContainer is verified on a box (docs/39).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from win32_fakes import FakeWin32

from fl_mcp.errors import ToolError
from fl_mcp.kernel.launch import KERNEL_JOB, PosixLauncher, Workspace
from fl_mcp.kernel.windows import INTERNET_CLIENT_SID, AppContainerLauncher
from fl_mcp.workspace.runner import TOOL_JOB, OpTimeout, WorkspaceRunner

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher and shell syntax")


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace.create(tmp_path / "root")


@pytest.fixture
def runner(ws):
    runner = WorkspaceRunner(ws, PosixLauncher(), os.environ)
    yield runner
    runner.close()


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _shell(runner: WorkspaceRunner, command: str, timeout_s: int = 10) -> dict:
    return runner.call(
        "shell", {"command": command, "timeout_s": timeout_s, "log_number": 1}, timeout=timeout_s + 15
    )


# -- POSIX: real processes --------------------------------------------------------------


@posix_only
def test_a_call_runs_in_its_own_process_and_answers(runner, ws):
    assert runner.call("write_file", {"path": "a.txt", "content": "hi"}, timeout=30)["created"] is True
    result = _shell(runner, "cat a.txt; echo oops >&2; exit 2")
    assert (result["exit_code"], result["stdout"], result["stderr"]) == (2, "hi", "oops\n")


@posix_only
def test_a_timed_out_command_and_everything_it_started_are_stopped(runner, ws):
    started = time.monotonic()
    result = _shell(runner, "sleep 60 & echo $! > bg.pid; sleep 60", timeout_s=1)
    assert result["timed_out"] is True and result["exit_code"] is None
    assert "1 s limit" in result["note"]
    assert time.monotonic() - started < 10
    assert _gone(int((ws.work / "bg.pid").read_text()))


@posix_only
def test_nothing_a_command_starts_outlives_its_call(runner, ws):
    started = time.monotonic()
    result = _shell(runner, "sleep 60 & echo $! > bg.pid; echo started")
    assert result["exit_code"] == 0 and result["stdout"] == "started\n"
    assert "left a process running" in result["note"]
    assert time.monotonic() - started < 10
    assert _gone(int((ws.work / "bg.pid").read_text()))


@posix_only
def test_the_op_sees_only_the_built_environment(ws):
    """Credentials and FLMCP_* wiring in the server's environment never reach a call."""
    environ = {**os.environ, "ANTHROPIC_API_KEY": "sk-not-for-models", "FLMCP_SPOOL_ROOT": "/spool"}
    runner = WorkspaceRunner(ws, PosixLauncher(), environ)
    try:
        out = _shell(runner, "env")["stdout"]
    finally:
        runner.close()
    assert "sk-not-for-models" not in out and "FLMCP_" not in out
    assert f"HOME={ws.work}" in out


@posix_only
def test_files_the_model_planted_are_never_imported_by_the_op(runner, ws):
    """-I keeps the workspace and its pip target off the op's sys.path."""
    (ws.work / "json.py").write_text("raise SystemExit('planted json imported')\n")
    (ws.packages / "json.py").write_text("raise SystemExit('planted json imported')\n")
    assert runner.call("glob", {"pattern": "*.py"}, timeout=30)["paths"] == ["json.py"]


@posix_only
def test_python_in_shell_imports_what_pip_installed_in_the_workspace_after_the_engines_own(runner, ws):
    """pip installs into the workspace (PIP_TARGET); a python started in shell must import it,
    and must still prefer the engine's packages over a same-named one installed there."""
    (ws.packages / "groove_tools.py").write_text("SWING = 0.56\n")
    (ws.packages / "pydantic.py").write_text("raise SystemExit('the workspace copy shadowed the engine')\n")
    out = _shell(
        runner, "python -c 'import groove_tools, pydantic; print(groove_tools.SWING, pydantic.VERSION)'"
    )
    assert out["exit_code"] == 0, out["stderr"]
    assert out["stdout"].startswith("0.56 ")


@posix_only
def test_a_planted_tmp_file_cannot_break_later_calls(runner, ws):
    """The server used to mkdir work/.tmp on every launch; a file there broke every call."""
    _shell(runner, "rm -rf .tmp && echo x > .tmp")
    assert runner.call("glob", {"pattern": "*"}, timeout=30)["paths"] == [".tmp"]
    assert _shell(runner, "echo ok")["stdout"] == "ok\n"


def test_op_refusals_arrive_as_invalid_argument_with_their_fix(runner, ws):
    (ws.work / "a.txt").write_text("x x")
    with pytest.raises(ToolError) as caught:
        runner.call("edit_file", {"path": "a.txt", "old_string": "x", "new_string": "y"}, timeout=30)
    assert caught.value.category == "invalid_argument"
    assert "appears 2 times" in caught.value.message and "replace_all" in caught.value.fix


class _ScriptLauncher(PosixLauncher):
    """Runs a script in place of the op, to play an op that misbehaves."""

    def __init__(self, script: str) -> None:
        self.script = script

    def __call__(self, argv, *, workspace, env, limits=KERNEL_JOB):
        return super().__call__([sys.executable, "-c", self.script], workspace=workspace, env=env)


@posix_only
def test_an_op_past_its_deadline_is_stopped_with_its_tree(ws):
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('child.pid', 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    runner = WorkspaceRunner(ws, _ScriptLauncher(script), os.environ)
    with pytest.raises(OpTimeout):
        runner.call("glob", {"pattern": "*"}, timeout=1.5)
    assert _gone(int((ws.work / "child.pid").read_text()))


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("import sys; sys.exit(3)", "exit code 3"),
        ("print('not json')", "without an answer"),
        ("print('{\"ok\": [1, 2]}')", "without an answer"),
    ],
)
def test_an_op_that_dies_or_answers_nonsense_is_an_internal_error(ws, script, expected):
    runner = WorkspaceRunner(ws, _ScriptLauncher(script), os.environ)
    with pytest.raises(ToolError) as caught:
        runner.call("glob", {"pattern": "*"}, timeout=30)
    assert caught.value.category == "internal_error"
    assert expected in caught.value.message


def test_a_launch_failure_is_kernel_unavailable_and_retryable(ws):
    class _NoLaunch(PosixLauncher):
        def __call__(self, argv, *, workspace, env, limits=KERNEL_JOB):
            raise OSError("no interpreter")

    with pytest.raises(ToolError) as caught:
        WorkspaceRunner(ws, _NoLaunch(), os.environ).call("glob", {"pattern": "*"}, timeout=30)
    assert caught.value.category == "kernel_unavailable" and caught.value.retryable
    assert "no interpreter" in caught.value.message


class _ConfinedClaim(PosixLauncher):
    """Claims confinement it does not have: the probe must catch the lie."""

    confined = True


def test_a_launcher_claiming_confinement_must_pass_the_probe_before_anything_runs(ws):
    runner = WorkspaceRunner(ws, _ConfinedClaim(), os.environ)
    for _ in range(2):
        with pytest.raises(ToolError) as caught:
            runner.call("write_file", {"path": "ran.txt", "content": "x"}, timeout=30)
        assert caught.value.category == "kernel_unavailable" and not caught.value.retryable
        assert "canary_readable=True" in caught.value.message
    assert not (ws.work / "ran.txt").exists()


def test_a_closed_runner_refuses_calls(ws):
    runner = WorkspaceRunner(ws, PosixLauncher(), os.environ)
    runner.close()
    with pytest.raises(ToolError) as caught:
        runner.call("glob", {"pattern": "*"}, timeout=30)
    assert caught.value.category == "kernel_unavailable"


def test_shell_numbers_count_up_per_session(runner):
    assert [runner.next_shell_number() for _ in range(3)] == [1, 2, 3]


# -- Windows orchestration over the recorded Win32 calls ------------------------------------

PROBE_OK = {"ok": {"canary_readable": False, "imds_reachable": False}}


class AnsweringWin32(FakeWin32):
    """Each launched process writes its scripted answer and exits; None hangs until its job ends."""

    def __init__(self, answers: list[dict | None]) -> None:
        super().__init__()
        self.answers = list(answers)
        self.command_lines: list[str] = []
        self.exited: set[int] = set()
        self._next = 100

    def create_process(self, command_line, *, cwd, env_block, appcontainer_sid, capabilities, std_handles):
        super().create_process(
            command_line,
            cwd=cwd,
            env_block=env_block,
            appcontainer_sid=appcontainer_sid,
            capabilities=capabilities,
            std_handles=std_handles,
        )
        self.command_lines.append(command_line)
        process, self._next = self._next, self._next + 2
        answer = self.answers.pop(0)
        if answer is not None:
            os.write(std_handles[1] - 1000, json.dumps(answer).encode())
            self.exited.add(process)
        return process, process + 1, 5000 + process

    def terminate_job(self, job):
        self._record("terminate_job", job)
        self.exited.update(range(100, self._next, 2))

    def wait(self, handle, timeout_ms):
        if handle not in self.exited and timeout_ms:
            time.sleep(min(timeout_ms, 50) / 1000)
        return handle in self.exited


def _windows_runner(ws, api, **kwargs) -> WorkspaceRunner:
    return WorkspaceRunner(
        ws, AppContainerLauncher(api), {"PATH": r"C:\py", "SYSTEMROOT": r"C:\Windows"}, **kwargs
    )


def test_workspace_calls_run_in_the_container_in_their_own_smaller_job(ws):
    api = AnsweringWin32([PROBE_OK, {"ok": {"path": "a.txt", "bytes": 2, "created": True}}])
    runner = _windows_runner(ws, api)
    assert runner.confined
    assert runner.call("write_file", {"path": "a.txt", "content": "hi"}, timeout=30)["created"] is True
    assert [c for c in api.calls if c[0] == "create_job"] == [("create_job", TOOL_JOB)] * 2  # probe, then op
    assert TOOL_JOB.memory_bytes == 2 << 30 and TOOL_JOB.processes == 16
    assert all(line.endswith("-I -m fl_mcp.workspace.op") for line in api.command_lines)
    assert api.request["capabilities"] == (INTERNET_CLIENT_SID,)
    assert api.request["cwd"] == ws.work
    assert len([c for c in api.calls if c[0] == "grant"]) == 2  # once for the workspace, not per call
    order = [c[0] for c in api.calls if c[0] in {"create_process", "assign", "resume", "terminate_job"}]
    assert (
        order == ["create_process", "assign", "resume", "terminate_job"] * 2
    )  # each tree ends with its call


def test_the_probe_runs_once_before_the_first_call(ws):
    api = AnsweringWin32(
        [PROBE_OK, {"ok": {"paths": [], "truncated": False}}, {"ok": {"paths": [], "truncated": False}}]
    )
    runner = _windows_runner(ws, api)
    runner.call("glob", {"pattern": "*"}, timeout=30)
    runner.call("glob", {"pattern": "*"}, timeout=30)
    assert len(api.command_lines) == 3


@pytest.mark.parametrize("breach", ["canary_readable", "imds_reachable"])
def test_a_failed_probe_refuses_every_call_and_launches_nothing_more(ws, breach):
    api = AnsweringWin32([{"ok": {"canary_readable": False, "imds_reachable": False, breach: True}}])
    runner = _windows_runner(ws, api)
    for _ in range(2):
        with pytest.raises(ToolError) as caught:
            runner.call("shell", {"command": "whoami", "timeout_s": 5, "log_number": 1}, timeout=30)
        assert caught.value.category == "kernel_unavailable" and not caught.value.retryable
        assert f"{breach}=True" in caught.value.message
    assert len(api.command_lines) == 1  # only the probe ever ran


def test_a_probe_that_cannot_finish_is_retried_on_the_next_call(ws):
    api = AnsweringWin32([None, PROBE_OK, {"ok": {"paths": [], "truncated": False}}])
    runner = _windows_runner(ws, api, probe_timeout=0.2)
    with pytest.raises(ToolError) as caught:
        runner.call("glob", {"pattern": "*"}, timeout=30)
    assert caught.value.category == "kernel_unavailable" and caught.value.retryable
    assert "did not finish in time" in caught.value.message
    assert ("terminate_job", 7) in api.calls
    assert runner.call("glob", {"pattern": "*"}, timeout=30)["paths"] == []


def test_a_call_past_its_deadline_terminates_its_job_and_closes_its_handles(ws):
    api = AnsweringWin32([PROBE_OK, None])
    runner = _windows_runner(ws, api)
    with pytest.raises(OpTimeout):
        runner.call("shell", {"command": "ping -t 127.0.0.1", "timeout_s": 1, "log_number": 1}, timeout=0.2)
    hung = 102  # the second launch's process handle: the op after the probe
    tail = api.calls[api.calls.index(("assign", 7, hung)) :]
    assert ("terminate_job", 7) in tail
    assert ("close", hung) in tail and ("close", 7) in tail


def test_closing_withdraws_the_workspace_grants(ws):
    api = AnsweringWin32([])
    _windows_runner(ws, api).close()
    assert [c[0] for c in api.calls if c[0] == "revoke"] == ["revoke", "revoke"]


def test_the_request_names_the_session_root(ws, monkeypatch):
    """The op finds the workspace (and its canary) from the root the server sends."""
    sent: list[bytes] = []
    from fl_mcp.workspace import runner as runner_module

    monkeypatch.setattr(runner_module, "_write_all", lambda stream, data: sent.append(data))
    api = AnsweringWin32([PROBE_OK])
    _windows_runner(ws, api)._verify_boundary()
    assert json.loads(sent[0]) == {"op": "probe", "root": str(ws.root), "args": {}}
    assert Path(json.loads(sent[0])["root"]) == ws.root
