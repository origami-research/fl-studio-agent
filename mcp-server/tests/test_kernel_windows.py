"""The Windows AppContainer launch, with the Win32 calls injected.

Real Windows verification is a box run (docs/38). These tests pin what the
orchestration asks Windows for: the grants that define the boundary, the one
capability, the suspended-then-jobbed start, and cleanup when a step fails.
"""

from __future__ import annotations

import os

import pytest

from fl_mcp.kernel.launch import Workspace
from fl_mcp.kernel.windows import (
    APPCONTAINER_NAME,
    INTERNET_CLIENT_SID,
    KERNEL_JOB_MEMORY_BYTES,
    KERNEL_JOB_PROCESSES,
    RENDERS_GRANT,
    WORK_GRANT,
    AppContainerLauncher,
    env_block,
    job_limits,
)

SID = "S-1-15-2-1-2-3-4-5-6-7"


class FakeWin32:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        self.alive = True

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name == self.fail:
            raise OSError(f"{name} failed")

    def appcontainer_sid(self, name):
        self._record("appcontainer_sid", name)
        return SID

    def grant(self, path, sid, rights):
        self._record("grant", path, sid, rights)

    def revoke(self, path, sid):
        self._record("revoke", path, sid)

    def inheritable_handle(self, fd):
        return 1000 + fd

    def create_job(self):
        self._record("create_job")
        return 7

    def create_process(self, command_line, *, cwd, env_block, appcontainer_sid, capabilities, std_handles):
        self.request = {
            "command_line": command_line,
            "cwd": cwd,
            "env_block": env_block,
            "appcontainer_sid": appcontainer_sid,
            "capabilities": tuple(capabilities),
            "std_handles": std_handles,
        }
        self._record("create_process")
        return 11, 12, 4242

    def assign(self, job, process):
        self._record("assign", job, process)

    def resume(self, thread):
        self._record("resume", thread)

    def terminate_process(self, process):
        self._record("terminate_process", process)

    def terminate_job(self, job):
        self._record("terminate_job", job)
        self.alive = False

    def wait(self, handle, timeout_ms):
        return not self.alive

    def exit_code(self, process):
        return 1

    def close(self, handle):
        self._record("close", handle)


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace.create(tmp_path / "kernel root")


def _launch(api, workspace):
    argv = [r"C:\FLMCP\venv\Scripts\python.exe", "-I", "-m", "fl_mcp.kernel.child"]
    return AppContainerLauncher(api)(argv, workspace=workspace, env={"PATH": r"C:\py", "HOME": "w"})


def test_grants_define_the_boundary_and_nothing_else(workspace):
    """Read/write work, read renders: every other path is denied by AppContainer default."""
    api = FakeWin32()
    _launch(api, workspace)
    assert [c for c in api.calls if c[0] in {"appcontainer_sid", "grant"}] == [
        ("appcontainer_sid", APPCONTAINER_NAME),
        ("grant", workspace.work, SID, WORK_GRANT),
        ("grant", workspace.renders, SID, RENDERS_GRANT),
    ]
    assert WORK_GRANT == "(OI)(CI)M" and RENDERS_GRANT == "(OI)(CI)RX"


def test_process_starts_in_the_container_with_only_internet_client(workspace):
    api = FakeWin32()
    process = _launch(api, workspace)
    assert api.request["appcontainer_sid"] == SID
    assert api.request["capabilities"] == (INTERNET_CLIENT_SID,) == ("S-1-15-3-1",)
    assert api.request["cwd"] == workspace.work
    assert api.request["command_line"] == r"C:\FLMCP\venv\Scripts\python.exe -I -m fl_mcp.kernel.child"
    assert api.request["env_block"] == "HOME=w\0PATH=C:\\py\0"
    assert process.pid == 4242
    assert all(handle >= 1000 for handle in api.request["std_handles"])


def test_kernel_joins_its_job_before_it_runs(workspace):
    """Suspended start, then the job, then resume: no child of the kernel escapes the kill."""
    api = FakeWin32()
    _launch(api, workspace)
    order = [c[0] for c in api.calls if c[0] in {"create_job", "create_process", "assign", "resume", "close"}]
    assert order == ["create_job", "create_process", "assign", "resume", "close"]
    assert ("assign", 7, 11) in api.calls


def test_kill_terminates_the_whole_job_and_reaping_closes_the_handles(workspace):
    api = FakeWin32()
    process = _launch(api, workspace)
    assert process.poll() is None
    process.kill()
    assert ("terminate_job", 7) in api.calls
    assert process.wait(1) == 1
    assert ("close", 11) in api.calls and ("close", 7) in api.calls  # no handle leak per restart
    closes = len([c for c in api.calls if c[0] == "close"])
    assert process.wait() == 1 and process.poll() == 1
    process.kill()
    assert len([c for c in api.calls if c[0] == "close"]) == closes  # reaped once


def test_release_withdraws_the_session_grants(workspace):
    """All sessions share one SID, so a finished workspace must stop being reachable."""
    api = FakeWin32()
    AppContainerLauncher(api).release(workspace)
    assert [c for c in api.calls if c[0] == "revoke"] == [
        ("revoke", workspace.work, SID),
        ("revoke", workspace.renders, SID),
    ]


def test_job_limits_protect_fl_from_the_kernel_tree():
    basic = job_limits().BasicLimitInformation
    assert basic.LimitFlags & 0x2000 and basic.LimitFlags & 0x400  # kill on close, no WER dialog
    assert basic.LimitFlags & 0x200 and job_limits().JobMemoryLimit == KERNEL_JOB_MEMORY_BYTES
    assert basic.LimitFlags & 0x8 and basic.ActiveProcessLimit == KERNEL_JOB_PROCESSES
    assert basic.LimitFlags & 0x20 and basic.PriorityClass == 0x4000  # below normal: FL's audio wins


def test_a_failed_job_assignment_kills_the_suspended_process(workspace):
    api = FakeWin32(fail="assign")
    with pytest.raises(OSError, match="assign failed"):
        _launch(api, workspace)
    names = [c[0] for c in api.calls]
    assert "resume" not in names
    assert names[-4:] == ["close", "terminate_process", "close", "close"]  # thread, process, process, job


def test_the_host_keeps_only_its_pipe_ends(workspace):
    """The child's ends close in the server, so the kernel's exit reaches the host as EOF."""
    api = FakeWin32()
    process = _launch(api, workspace)
    for handle in api.request["std_handles"]:
        with pytest.raises(OSError):
            os.fstat(handle - 1000)
    assert not process.stdin.closed and not process.stdout.closed and not process.stderr.closed


def test_env_block_is_sorted_case_insensitively_and_nul_terminated():
    assert env_block({"b": "2", "A": "1", "PATH": "x"}) == "A=1\0b=2\0PATH=x\0"
