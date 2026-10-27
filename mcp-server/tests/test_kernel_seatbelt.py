"""The macOS boundary: the run kernel and the workspace tools under Seatbelt (docs/38).

The profile tests run everywhere and pin what the generated profile allows.
The darwin tests run real cells and shell commands through ``sandbox-exec``
and prove the contract the Windows AppContainer keeps: the workspace and the
public internet are reachable; the user's home, other workspaces, this
machine's own services, other processes and everything outside the
workspace are not; the fl calls still reach the server over the kernel's
pipes; and a stopped call takes its whole process tree with it.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sdk_fakes import DEFAULT_CHANNELS, FakeSession
from test_kernel import _wheel

from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import Workspace
from fl_mcp.kernel.seatbelt import SANDBOX_EXEC, Profile, SeatbeltLauncher, _Reach, build_profile
from fl_mcp.schemas import Target
from fl_mcp.store import Store
from fl_mcp.workspace.runner import WorkspaceRunner

darwin = pytest.mark.platform("darwin")
network = pytest.mark.skipif(os.environ.get("FLMCP_NETWORK_TESTS") != "1", reason="set FLMCP_NETWORK_TESTS=1")


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace.create(tmp_path / "kernel")


# -- the generated profile ------------------------------------------------------------------


def _profile(workspace: Workspace, runtime: tuple[Path, ...] = ()) -> Profile:
    return build_profile(workspace, runtime)


def _param(profile: Profile, path: Path) -> str:
    return next(name for name, value in profile.params if value == str(path))


def test_the_profile_denies_by_default_and_lets_only_the_workspace_be_written(workspace):
    profile = _profile(workspace)
    lines = [line for line in profile.text.splitlines() if line and not line.startswith(";")]
    assert lines[:2] == ["(version 1)", "(deny default)"]
    writes = [line for line in lines if "file-write*" in line and "/dev/" not in line]
    assert writes == [f'(allow file-write* (subpath (param "{_param(profile, workspace.work)}")))']
    renders = f'(subpath (param "{_param(profile, workspace.renders)}"))'
    assert renders in next(line for line in lines if line.startswith('(allow file-read* (literal "/")'))
    assert all(value != str(workspace.canary) for _, value in profile.params)
    # The root itself is passed through (metadata), never readable as a tree.
    assert f'(subpath (param "{_param(profile, workspace.root)}"))' not in profile.text


def test_paths_reach_the_profile_only_as_parameters(tmp_path):
    """A workspace path with SBPL syntax in it must not change the profile."""
    hostile = Workspace.create(tmp_path / 'a") (allow default) ("')
    profile = _profile(hostile)
    assert "allow default" not in profile.text
    assert (str(hostile.work)) in dict(profile.params).values()
    command = profile.command(["python", "-I"])
    assert command[0] == str(SANDBOX_EXEC) and command[-3:] == ["--", "python", "-I"]
    assert f"P0={hostile.work}" in command


def test_later_rules_carve_the_exceptions_out_of_the_allows(workspace):
    """Seatbelt applies the last matching rule, so each deny must follow the allow it narrows."""
    lines = _profile(workspace).text.splitlines()
    assert lines.index('(deny network-outbound (remote ip "localhost:*"))') > lines.index(
        '(allow network-outbound (remote ip "*:*"))'
    )
    assert lines.index("(allow process-info* (target same-sandbox))") > lines.index("(deny process-info*)")
    data = next(i for i, line in enumerate(lines) if line.startswith("(deny file-read* process-exec"))
    assert data > next(i for i, line in enumerate(lines) if line.startswith("(allow file-read*"))
    # The only Unix socket the kernel may reach is the system DNS resolver.
    assert [line for line in lines if "unix-socket" in line] == [
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
    ]


def test_a_runtime_behind_symlinks_is_readable_through_every_link(tmp_path):
    """A uv venv's python links into a version folder that is itself a link; each hop must resolve."""
    real = tmp_path / "python-3.12.9"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.12").write_text("")
    (tmp_path / "python-3.12").symlink_to(real)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(tmp_path / "python-3.12" / "bin" / "python3.12")
    reach = _Reach()
    reach.add(venv / "python")
    resolved = real.resolve()
    assert reach.trees == {resolved / "bin" / "python3.12"}
    assert {venv / "python", tmp_path / "python-3.12"} <= reach.links
    assert resolved.parent in reach.dirs and Path("/") in reach.dirs


def test_nested_readable_trees_collapse_into_their_parent(tmp_path):
    reach = _Reach()
    reach.add(tmp_path)
    reach.add(tmp_path)  # the same tree twice
    (tmp_path / "inner").mkdir()
    reach.add(tmp_path / "inner")
    assert reach.roots() == {tmp_path.resolve()}


# -- the run kernel under the real sandbox ----------------------------------------------------


@pytest.fixture
def session(tmp_path) -> FakeSession:
    return FakeSession(store=Store("seatbelt", root=tmp_path / "store"), channels=DEFAULT_CHANNELS)


@pytest.fixture
def host(session, workspace) -> Iterator[KernelHost]:
    kernel = KernelHost(session, workspace, SeatbeltLauncher(), os.environ, cell_timeout=60.0)
    yield kernel
    kernel.close()


@contextlib.contextmanager
def _listener(host: str = "127.0.0.1") -> Iterator[int]:
    """A service on this machine the kernel must not reach, like the FL control broker."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.create_server((host, 0), family=family) as server:
        yield server.getsockname()[1]


def _lan_address() -> str | None:
    """This machine's own network address, which reaches services bound to every interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the route
        except OSError:
            return None
        address = probe.getsockname()[0]
    return None if address.startswith("127.") else address


def _denied(host: KernelHost, code: str) -> str:
    result = host.run(code)
    assert result.outcome == "error", result
    return result.traceback.strip().splitlines()[-1]


@darwin
def test_fl_calls_reach_the_server_over_the_pipes_while_loopback_is_denied(host, session):
    """The Bridge path is the server's: a confined cell's fl calls still work end to end."""
    with _listener() as port:
        code = (
            "bass = fl.track('Bass')\n"
            "receipt = bass.write([Note(key='C4', start=0.0, length=1.0)])\n"
            "wav = fl.render('mix.wav')\n"
            "receipt.total, fl.set_tempo(128.0), fl.tempo, wav.read_bytes()[:4]"
        )
        assert host.run(code).result == "(1, 128.0, 128.0, b'RIFF')"
        assert "PermissionError" in _denied(
            host, f"import socket\nsocket.create_connection(('127.0.0.1', {port}), timeout=3)"
        )
    assert [n.key for n in session.read_notes(Target(pattern=1, channel=1))] == [60]


@darwin
def test_no_address_of_this_machine_is_reachable(host):
    """Seatbelt's localhost covers IPv6 loopback and the machine's own address, not only 127.0.0.1."""
    targets = [("::1", "::1")]
    if (lan := _lan_address()) is not None:
        targets.append(("0.0.0.0", lan))  # a service bound to every interface
    for bind, connect in targets:
        with _listener(bind) as port:
            code = f"import socket\nsocket.create_connection(({connect!r}, {port}), timeout=3)"
            assert "PermissionError" in _denied(host, code), connect


@darwin
def test_the_kernel_cannot_write_outside_its_workspace(host, workspace, tmp_path):
    assert host.run("open('inside.txt', 'w').write('ok')").result == "2"
    for outside in (tmp_path / "outside.txt", workspace.renders / "x.wav", Path.home() / "flmcp-escape.txt"):
        assert "PermissionError" in _denied(host, f"open({str(outside)!r}, 'w')")
        assert not outside.exists()


@darwin
def test_the_kernel_cannot_read_the_users_home_or_its_secrets(host, workspace):
    home = Path.home()
    for code in (
        f"import os\nos.listdir({str(home)!r})",
        f"import os\nos.listdir({str(home / '.ssh')!r})",
        f"open({str(home / '.zshrc')!r}).read()",
        f"open({str(workspace.canary)!r}).read()",
        f"import os\nos.link({str(workspace.canary)!r}, 'canary-link')",
    ):
        assert "PermissionError" in _denied(host, code)


@darwin
def test_the_kernel_cannot_read_another_sessions_workspace(host, tmp_path):
    other = Workspace.create(tmp_path / "other-session")
    (other.work / "secret.txt").write_text("not yours")
    assert "PermissionError" in _denied(host, f"open({str(other.work / 'secret.txt')!r}).read()")


@darwin
def test_the_kernel_cannot_reach_other_processes(host):
    """Another process's command line and environment hold credentials; signals could stop the server."""
    code = (
        "import ctypes, os\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "mib = (ctypes.c_int * 3)(1, 49, os.getppid())  # KERN_PROCARGS2 of the server\n"
        "size = ctypes.c_size_t(1 << 16)\n"
        "buf = ctypes.create_string_buffer(size.value)\n"
        "libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0), ctypes.get_errno()"
    )
    assert host.run(code).result == "(-1, 1)"  # EPERM
    assert "PermissionError" in _denied(host, "import os\nos.kill(os.getppid(), 0)")


@darwin
def test_pip_installs_into_the_workspace_and_the_package_imports(host, workspace, tmp_path):
    wheels = workspace.renders  # readable by the kernel, written by the server
    wheel = _wheel(tmp_path)
    (wheels / wheel.name).write_bytes(wheel.read_bytes())
    installed = host.run(f"%pip install --no-index {wheels / wheel.name}")
    assert installed.outcome == "ok", installed
    assert host.run("import flmcp_probe_pkg\nflmcp_probe_pkg.VALUE").result == "42"


@darwin
@network
def test_pip_from_pypi_and_public_https_work_under_the_sandbox(host):
    assert host.run("%pip install -q six==1.17.0").outcome == "ok"
    assert host.run("import six\nsix.__version__").result == "'1.17.0'"
    fetch = "import urllib.request\nurllib.request.urlopen('https://pypi.org/simple/six/', timeout=20).status"
    assert host.run(fetch).result == "200"


@darwin
def test_a_killed_cell_takes_its_subprocesses_with_it(session, workspace):
    kernel = KernelHost(session, workspace, SeatbeltLauncher(), os.environ, cell_timeout=1.0, interrupt_grace=1.0)
    try:
        code = (
            "import subprocess, time\n"
            "child = subprocess.Popen(['/bin/sleep', '300'])\n"
            "open('child.pid', 'w').write(str(child.pid))\n"
            "time.sleep(300)"
        )
        assert kernel.run(code).outcome == "kernel_lost"
        assert _gone(int((workspace.work / "child.pid").read_text()))
    finally:
        kernel.close()


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# -- the workspace tools under the real sandbox ---------------------------------------------------


@pytest.fixture
def runner(workspace) -> Iterator[WorkspaceRunner]:
    runner = WorkspaceRunner(workspace, SeatbeltLauncher(), os.environ)
    yield runner
    runner.close()


def _shell(runner: WorkspaceRunner, command: str, timeout_s: int = 20) -> dict:
    return runner.call(
        "shell", {"command": command, "timeout_s": timeout_s, "log_number": 1}, timeout=timeout_s + 15
    )


@darwin
def test_workspace_tools_pass_the_probe_and_work_in_the_workspace(runner):
    assert runner.confined
    assert runner.call("write_file", {"path": "a.txt", "content": "hi"}, timeout=30)["created"] is True
    assert "hi" in runner.call("read_file", {"path": "a.txt"}, timeout=30)["content"]
    pool = "import multiprocessing as m\nif __name__ == '__main__':\n    print(m.Pool(2).map(abs, [-1, -2]))\n"
    runner.call("write_file", {"path": "pool.py", "content": pool}, timeout=30)
    out = _shell(runner, "cat a.txt && python -c 'import fl_mcp; print(1)' && python pool.py && git --version")
    assert out["exit_code"] == 0, out
    assert out["stdout"].startswith("hi1\n[1, 2]\ngit version")


@darwin
def test_workspace_tools_cannot_reach_outside_the_workspace(runner, workspace, tmp_path):
    from fl_mcp.errors import ToolError

    for path in (str(Path.home() / ".ssh" / "known_hosts"), str(workspace.canary)):
        with pytest.raises(ToolError, match=r"(?i)permission|not permitted"):
            runner.call("read_file", {"path": path}, timeout=30)
    outside = tmp_path / "outside.txt"
    with _listener() as port:
        out = _shell(
            runner,
            f"echo x > {outside}; ls {Path.home()}; "
            f"python -c 'import socket; socket.create_connection((\"127.0.0.1\", {port}), timeout=3)'; "
            "osascript -e 'tell application \"Finder\" to get version'; "
            "pbpaste; defaults read -g AppleLocale",
        )
    assert not outside.exists()
    assert out["stdout"] == ""  # no Finder version, no pasteboard, no user preferences
    assert out["stderr"].count("Operation not permitted") >= 3, out["stderr"]


@darwin
def test_a_shell_call_stops_everything_it_started(runner, workspace):
    result = _shell(runner, "sleep 60 & echo $! > bg.pid; echo started")
    assert result["stdout"] == "started\n"
    assert _gone(int((workspace.work / "bg.pid").read_text()))


@darwin
def test_a_missing_sandbox_exec_refuses_instead_of_running_unconfined(session, workspace, monkeypatch):
    from fl_mcp.errors import ToolError
    from fl_mcp.kernel import seatbelt

    monkeypatch.setattr(seatbelt, "SANDBOX_EXEC", Path("/nonexistent/sandbox-exec"))
    kernel = KernelHost(session, workspace, SeatbeltLauncher(), os.environ)
    with pytest.raises(ToolError) as caught:
        kernel.run("open('ran.txt', 'w')")
    assert caught.value.category == "kernel_unavailable" and "sandbox-exec" in caught.value.message
    assert not (workspace.work / "ran.txt").exists()


def test_the_seatbelt_launcher_says_it_confines(workspace):
    assert SeatbeltLauncher.confined is True
    assert sys.platform == "darwin" or SeatbeltLauncher().release(workspace) is None
