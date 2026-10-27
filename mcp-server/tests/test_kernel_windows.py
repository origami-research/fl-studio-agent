"""The Windows AppContainer launch, with the Win32 calls injected.

Real Windows verification is a box run (docs/38). These tests pin what the
orchestration asks Windows for: the grants that define the boundary, the one
capability, the suspended-then-jobbed start, and cleanup when a step fails.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from session_fakes import RENDER_SPAN
from win32_fakes import SID, FakeWin32

from fl_mcp import server
from fl_mcp.kernel.launch import KERNEL_JOB, Workspace
from fl_mcp.kernel.windows import (
    APPCONTAINER_NAME,
    INTERNET_CLIENT_SID,
    RENDERS_GRANT,
    RUNTIME_GRANT,
    WORK_GRANT,
    AppContainerLauncher,
    env_block,
    grants_read_execute,
    job_limits,
    runtime_dirs,
)


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


# A user's own install: uvx's venv in %LOCALAPPDATA%\uv\cache and uv's managed Python
# in %APPDATA%\uv\python, folders the user owns and no AppContainer can read.
USER_RUNTIME = (Path(r"C:\Users\u\AppData\Local\uv\cache\archive-v0\env"), Path(r"C:\Users\u\AppData\Roaming\uv\python\cpython-3.12"))


def test_a_user_install_runtime_is_granted_read_once_before_the_kernel_starts(workspace):
    """The public install path (2026-10-26 box run): the kernel exited 106 "No pyvenv.cfg file"
    because its own venv was unreadable inside the container, so `run` never worked on a
    user's Windows machine. The launcher grants the interpreter read and execute, once."""
    api = FakeWin32(runtime_readable=False)
    launcher = AppContainerLauncher(api, runtime=USER_RUNTIME)
    argv = ["python", "-I", "-m", "fl_mcp.kernel.child"]
    for _ in range(2):
        launcher(argv, workspace=workspace, env={})
    grants = [c for c in api.calls if c[0] == "grant"]
    assert grants[:2] == [("grant", USER_RUNTIME[0], SID, RUNTIME_GRANT), ("grant", USER_RUNTIME[1], SID, RUNTIME_GRANT)]
    assert len(grants) == 4  # the runtime and the workspace, each once
    names = [c[0] for c in api.calls]
    assert names.index("grant") < names.index("create_process")
    assert RUNTIME_GRANT == "(OI)(CI)RX"  # read and run, never write


def test_a_provisioned_runtime_is_left_alone(workspace):
    """A box grants its venv (C:/FLMCP/venv) and Program Files is readable by every container:
    the FL user cannot rewrite those ACLs and does not need to."""
    api = FakeWin32(runtime_readable=True)
    AppContainerLauncher(api, runtime=USER_RUNTIME)(["python"], workspace=workspace, env={})
    assert [c[1] for c in api.calls if c[0] == "readable"] == list(USER_RUNTIME)
    assert [c[1] for c in api.calls if c[0] == "grant"] == [workspace.work, workspace.renders]


def test_a_runtime_this_user_cannot_grant_fails_the_launch_and_says_how_to_fix_it(workspace):
    api = FakeWin32(fail="grant", runtime_readable=False)
    with pytest.raises(OSError, match=r"not readable by its AppContainer.*Box contract"):
        AppContainerLauncher(api, runtime=USER_RUNTIME)(["python"], workspace=workspace, env={})
    assert "create_process" not in [c[0] for c in api.calls]


def test_runtime_dirs_are_this_interpreters_venv_and_base():
    import sys

    dirs = runtime_dirs()
    assert dirs[0] == Path(sys.prefix).resolve()
    assert set(dirs) == {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}


def test_read_execute_is_read_by_any_of_its_spellings():
    assert grants_read_execute(0x001200A9)  # (RX), what icacls and Program Files write
    assert grants_read_execute(0x001F01FF)  # (F)
    assert grants_read_execute(0x10000000)  # GENERIC_ALL
    assert grants_read_execute(0xA0000000)  # GENERIC_READ | GENERIC_EXECUTE
    assert not grants_read_execute(0x00120089)  # read without execute
    assert not grants_read_execute(0x80000000)  # GENERIC_READ alone


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
    limits = job_limits(KERNEL_JOB)
    basic = limits.BasicLimitInformation
    assert basic.LimitFlags & 0x2000 and basic.LimitFlags & 0x400  # kill on close, no WER dialog
    assert basic.LimitFlags & 0x200 and limits.JobMemoryLimit == KERNEL_JOB.memory_bytes == 4 << 30
    assert basic.LimitFlags & 0x8 and basic.ActiveProcessLimit == KERNEL_JOB.processes == 32
    assert basic.LimitFlags & 0x20 and basic.PriorityClass == 0x4000  # below normal: FL's audio wins


def test_the_kernel_launch_asks_for_the_kernel_job(workspace):
    """The kernel's own launch keeps its limits; the workspace tools ask for smaller ones."""
    api = FakeWin32()
    _launch(api, workspace)
    assert ("create_job", KERNEL_JOB) in api.calls


def test_a_workspace_is_granted_once_and_a_release_is_final(workspace):
    """icacls rewrites every file below a granted folder, and the workspace tools launch
    once per call: regranting on each launch would walk .packages every time. A launch
    racing shutdown must not grant a finished session again (review, 2026-10-22)."""
    api = FakeWin32()
    launcher = AppContainerLauncher(api)
    argv = ["python", "-I", "-m", "fl_mcp.workspace.op"]
    for _ in range(3):
        launcher(argv, workspace=workspace, env={})
    assert len([c for c in api.calls if c[0] == "grant"]) == 2  # work and renders, once
    launcher.release(workspace)
    with pytest.raises(OSError, match="session is over"):
        launcher(argv, workspace=workspace, env={})
    assert len([c for c in api.calls if c[0] == "grant"]) == 2


def test_release_attempts_both_revokes_when_the_first_fails(workspace):
    api = FakeWin32(fail="revoke")
    with pytest.raises(OSError):
        AppContainerLauncher(api).release(workspace)
    assert [c[1] for c in api.calls if c[0] == "revoke"] == [workspace.work, workspace.renders]


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


# -- renders the kernel can open ------------------------------------------------------

_PRIVATE = 0o700


def _container_can_read(path: Path, grants: dict[Path, str], private_dirs: set[Path]) -> bool:
    """Windows ACL inheritance as the kernel meets it.

    A new file takes the inheritable grants of the folders above it, up to the
    nearest folder that was granted explicitly. A folder made private on the way
    (mode 0o700: an owner-only, non-inheriting ACL since Python 3.12.4) cuts
    that chain, so nothing below it reaches the container.
    """
    for folder in path.parents:
        if folder in private_dirs:
            return False
        rights = grants.get(folder)
        if rights is not None:
            return "(OI)" in rights and ("RX" in rights or "M" in rights)
    return False


class _Recorder:
    """The server's own folder creation during a render, as Windows would see it."""

    def __init__(self, monkeypatch) -> None:
        self.private: set[Path] = set()
        real_mkdir = os.mkdir

        def mkdir(path, mode=0o777, *, dir_fd=None):
            if mode == _PRIVATE:
                self.private.add(Path(path).resolve())
            real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "mkdir", mkdir)


class _RenderSession:
    def render_wav(self, output: Path):
        from fl_mcp.schemas import RenderReceipt

        output.write_bytes(b"RIFF....WAVE")
        return RenderReceipt(path=output, span=RENDER_SPAN)


def test_every_render_the_server_writes_is_one_the_kernel_can_open(workspace, monkeypatch):
    """Live run 2026-10-19: on locked-down Windows boxes a model rendered, then could not
    open the file. The render tool wrote into a tempfile.mkdtemp folder inside renders,
    which the server (unlike the kernel) does not patch, so the folder was owner-only and
    the container's inherited read grant never reached the WAV."""
    api = FakeWin32()
    _launch(api, workspace)
    grants = {c[1]: c[3] for c in api.calls if c[0] == "grant"}
    recorder = _Recorder(monkeypatch)
    session = _RenderSession()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"session": session, "kernel": SimpleNamespace(workspace=workspace)}
        )
    )

    tool = Path(server.render(ctx).path)
    fl_render = workspace.output_path(Path("mix.wav"))  # where fl.render("mix.wav") lands
    session.render_wav(fl_render)
    for path in (tool, fl_render):
        assert path.is_file()
        assert _container_can_read(path, grants, recorder.private), path
        assert path.parent == workspace.renders, "no folder between renders and the WAV"


def test_the_acl_model_rejects_a_render_in_a_private_folder(workspace):
    """The check above would have caught the old layout."""
    grants = {workspace.renders: RENDERS_GRANT, workspace.work: WORK_GRANT}
    private = workspace.renders / "render-abc"
    assert _container_can_read(workspace.renders / "mix.wav", grants, set())
    assert not _container_can_read(private / "render.wav", grants, {private})
    assert not _container_can_read(workspace.root / "boundary-canary", grants, set())
