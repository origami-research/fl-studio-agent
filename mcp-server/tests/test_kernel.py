"""The run kernel: a real child process driven through KernelHost.

Every test here boots an actual ``python -m fl_mcp.kernel.child`` against the
sdk_fakes session, so persistence, isolation, timeouts, and the fl wire are
the shipped code paths. Network is never touched except by the opt-in test.
"""

from __future__ import annotations

import ast
import contextlib
import os
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sdk_fakes import DEFAULT_CHANNELS, FakeSession

from fl_mcp import errors
from fl_mcp.errors import ToolError
from fl_mcp.flp import Effect
from fl_mcp.kernel import wire
from fl_mcp.kernel.cell import expand_magics
from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import KERNEL_SITE, PosixLauncher, Workspace
from fl_mcp.kernel.private_dirs import share_private_dirs
from fl_mcp.schemas import Channel, RenderReceipt, Target
from fl_mcp.store import Store

SDK_SOURCE = Path(__file__).resolve().parents[1] / "src" / "fl_mcp" / "sdk.py"


class _Session(FakeSession):
    """The sdk_fakes session plus the seams these tests observe."""

    def __init__(self, *args, render_seconds: float = 0.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.render_seconds = render_seconds
        self.file_edit_log: list[str] = []
        self.file_edit_exit_failures = 0

    def render_wav(self, output: Path) -> RenderReceipt:
        time.sleep(self.render_seconds)
        return super().render_wav(output)

    @contextlib.contextmanager
    def file_edit(self) -> Iterator[None]:
        self.file_edit_log.append("enter")
        try:
            yield
        finally:
            self.file_edit_log.append("exit")
            if self.file_edit_exit_failures:
                self.file_edit_exit_failures -= 1
                raise errors.ToolError("fl_relaunch_required", "FL did not come back", "relaunch FL")


@pytest.fixture
def session(tmp_path) -> _Session:
    return _Session(store=Store("kernel", root=tmp_path / "store"), channels=DEFAULT_CHANNELS)


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace.create(tmp_path / "kernel")


def _host(session, workspace, environ=None, **budgets) -> KernelHost:
    return KernelHost(
        session, workspace, PosixLauncher(), os.environ if environ is None else environ, **budgets
    )


@pytest.fixture
def host(session, workspace) -> Iterator[KernelHost]:
    kernel = _host(session, workspace, cell_timeout=2.0, interrupt_grace=1.0)
    yield kernel
    kernel.close()


# -- a kernel is a notebook ---------------------------------------------------


def test_namespace_persists_across_cells(host):
    assert host.run("import math\ndef double(x):\n    return 2 * x\nvalue = 21").outcome == "ok"
    assert host.run("double(value), math.pi > 3").result == "(42, True)"


def test_workspace_is_cwd_home_and_importable(host, workspace):
    """Model files live in the workspace, and a module written there imports like a notebook's."""
    host.run("from pathlib import Path\nPath('helper.py').write_text('ANSWER = 7')")
    result = host.run("import os, helper\nos.getcwd(), os.environ['HOME'], helper.ANSWER")
    assert result.result == repr((str(workspace.work), str(workspace.work), 7)), result


def test_kernel_environment_carries_no_server_secrets(session, workspace):
    """The kernel gets a built environment, not the server's: credentials stay out."""
    environ = {**os.environ, "AWS_SECRET_ACCESS_KEY": "do-not-leak", "FLMCP_SPOOL_ROOT": "/spool"}
    kernel = _host(session, workspace, environ)
    try:
        result = kernel.run(
            "import os\nos.environ.get('AWS_SECRET_ACCESS_KEY'), os.environ.get('FLMCP_SPOOL_ROOT')"
        )
    finally:
        kernel.close()
    assert result.result == "(None, None)"


def test_stray_fd_writes_and_stdin_reads_cannot_touch_the_wire(host):
    """fd 1 and fd 0 were the wire; the kernel moved it, so model code can use both freely."""
    assert host.run("import os\nos.write(1, b'not json\\n')\n5").result == "5"
    assert "EOFError" in host.run("input()").traceback
    assert host.run("6").result == "6"


def test_stderr_is_part_of_the_cell_output(host):
    assert host.run("import sys\nprint('to err', file=sys.stderr)").stdout == "to err\n"


# -- magics and packages ------------------------------------------------------


def test_magic_lines_become_calls_and_keep_line_numbers():
    code = "!ls -la\nif True:\n    %pip install six\nx = '!not a magic'"
    assert expand_magics(code).splitlines() == [
        "__fl_shell__('ls -la')",
        "if True:",
        "    __fl_pip__('install six')",
        "x = '!not a magic'",
    ]


def test_shell_magic_output_and_failure(host):
    assert host.run("!echo from-shell").stdout == "from-shell\n"
    failed = host.run("!exit 3")
    assert failed.outcome == "error"
    assert "CalledProcessError" in failed.traceback


def _wheel(directory: Path) -> Path:
    """A minimal pure-Python wheel, so pip installs it with no network."""
    name, version = "flmcp_probe_pkg", "1.0"
    files = {
        f"{name}/__init__.py": "VALUE = 42\n",
        f"{name}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{name}-{version}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = f"{name}-{version}.dist-info/RECORD"
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in files.items():
            archive.writestr(member, text)
        archive.writestr(record, "".join(f"{member},,\n" for member in [*files, record]))
    return path


def test_pip_magic_installs_into_the_workspace_and_imports_next_cell(host, workspace, tmp_path):
    wheel = _wheel(tmp_path)
    installed = host.run(f"%pip install --no-index {wheel}")
    assert installed.outcome == "ok", installed
    assert (workspace.packages / "flmcp_probe_pkg" / "__init__.py").is_file()
    assert host.run("import flmcp_probe_pkg\nflmcp_probe_pkg.VALUE").result == "42"


@pytest.mark.skipif(os.environ.get("FLMCP_NETWORK_TESTS") != "1", reason="set FLMCP_NETWORK_TESTS=1")
def test_pip_magic_installs_from_pypi(session, workspace):
    kernel = _host(session, workspace, cell_timeout=120.0)
    try:
        assert kernel.run("%pip install six==1.17.0").outcome == "ok"
        assert kernel.run("import six\nsix.__version__").result == "'1.17.0'"
    finally:
        kernel.close()


# -- isolation: a cell cannot take the server down ------------------------------


def test_system_exit_is_an_error_and_the_kernel_survives(host):
    host.run("kept = 'yes'")
    for code in ("raise SystemExit(3)", "import sys\nsys.exit()"):
        result = host.run(code)
        assert result.outcome == "error"
        assert "SystemExit" in result.traceback
    assert host.run("kept").result == "'yes'"


def test_os_exit_loses_the_kernel_and_the_next_run_starts_fresh(host):
    host.run("kept = 'yes'")
    lost = host.run("import os\nos._exit(4)")
    assert lost.outcome == "kernel_lost"
    assert "exited with code 4" in lost.traceback
    fresh = host.run("kept")
    assert fresh.outcome == "error" and "NameError" in fresh.traceback


def test_a_runaway_loop_is_interrupted_and_state_survives(session, workspace):
    kernel = _host(session, workspace, cell_timeout=0.5, interrupt_grace=2.0)
    try:
        kernel.run("kept = 'yes'")
        result = kernel.run("while True:\n    pass")
        assert result.outcome == "interrupted"
        assert "KeyboardInterrupt" in result.traceback
        assert kernel.run("kept").result == "'yes'"
    finally:
        kernel.close()


def test_a_cell_that_ignores_the_interrupt_is_killed_and_replaced(session, workspace):
    """A blocking C call never sees the interrupt; the kill is the guarantee."""
    kernel = _host(session, workspace, cell_timeout=0.3, interrupt_grace=0.3)
    try:
        kernel.run("kept = 'yes'")
        lost = kernel.run("import time\ntime.sleep(30)")
        assert lost.outcome == "kernel_lost"
        assert "did not stop" in lost.traceback
        assert "NameError" in kernel.run("kept").traceback
        assert kernel.run("1 + 1").result == "2"
    finally:
        kernel.close()


def test_time_inside_fl_calls_is_not_charged_to_the_cell(tmp_path, workspace):
    """A render plays the whole arrangement; it must not count as runaway compute."""
    slow = _Session(store=Store("k", root=tmp_path / "s"), channels=DEFAULT_CHANNELS, render_seconds=1.0)
    kernel = _host(slow, workspace, cell_timeout=0.5, interrupt_grace=0.5)
    try:
        assert kernel.run("fl.render('mix.wav').name").result == "'mix.wav'"
    finally:
        kernel.close()


def test_kernel_that_cannot_start_reports_why(session, workspace):
    class _Crashing:
        confined = False

        def __call__(self, argv, *, workspace, env):
            script = "import sys; sys.stderr.write('boom on import\\n'); sys.exit(2)"
            return PosixLauncher()([sys.executable, "-c", script], workspace=workspace, env=env)

    kernel = KernelHost(session, workspace, _Crashing(), os.environ)
    with pytest.raises(ToolError) as caught:
        kernel.run("1")
    assert caught.value.category == "kernel_unavailable"
    assert "exited with code 2" in caught.value.message
    assert "boom on import" in caught.value.message


def test_an_unreadable_message_loses_the_kernel(session, workspace):
    """The server parses kernel output as untrusted data; a call to a non-SDK method is garbage."""

    class _Rogue:
        confined = False

        def __call__(self, argv, *, workspace, env):
            script = (
                "import sys\n"
                'print(\'{"op": "ready", "pid": 1}\', flush=True)\n'
                "sys.stdin.readline()\n"
                'print(\'{"op": "call", "id": 1, "method": "close"}\', flush=True)\n'
                "sys.stdin.readline()\n"
            )
            return PosixLauncher()([sys.executable, "-c", script], workspace=workspace, env=env)

    kernel = KernelHost(session, workspace, _Rogue(), os.environ)
    result = kernel.run("x")
    assert result.outcome == "kernel_lost"
    assert "unreadable message" in result.traceback


def test_an_oversized_message_loses_the_kernel_without_exhausting_the_server(session, workspace):
    """The server bounds what it reads from the kernel; a runaway line is cut off, not buffered."""

    class _Flooder:
        confined = False

        def __call__(self, argv, *, workspace, env):
            script = (
                "import sys\n"
                'print(\'{"op": "ready", "pid": 1}\', flush=True)\n'
                "sys.stdin.readline()\n"
                "sys.stdout.buffer.write(b'x' * (65 << 20))\n"
                "sys.stdout.flush()\n"
            )
            return PosixLauncher()([sys.executable, "-c", script], workspace=workspace, env=env)

    result = KernelHost(session, workspace, _Flooder(), os.environ).run("x")
    assert result.outcome == "kernel_lost"
    assert "over 64 MiB" in result.traceback


# -- the OS boundary is verified before any cell runs ----------------------------


class _ConfinedClaim(PosixLauncher):
    """Claims confinement it does not have: the probe must catch the lie."""

    confined = True


def test_a_kernel_claiming_confinement_must_pass_the_boundary_probe(session, workspace):
    kernel = KernelHost(session, workspace, _ConfinedClaim(), os.environ)
    with pytest.raises(ToolError) as caught:
        kernel.run("1")
    assert caught.value.category == "kernel_unavailable"
    assert "canary_readable=True" in caught.value.message


# -- the fl wire ---------------------------------------------------------------------


def test_fl_calls_round_trip_to_the_server_session(host, session):
    code = (
        "bass = fl.track('Bass')\n"
        "receipt = bass.write([Note(key='C4', start=0.0, length=1.0), Note(key=64, start=1.0, length=0.5)])\n"
        "receipt.total, [n.key for n in bass.notes()], fl.set_tempo(128.0), fl.tempo"
    )
    assert host.run(code).result == "(2, [60, 64], 128.0, 128.0)"
    assert [n.key for n in session.read_notes(Target(pattern=1, channel=1))] == [60, 64]


def test_session_errors_reach_the_cell_as_catchable_tool_errors(host, session):
    session.fail_next_write = errors.ToolError("note_plane_unavailable", "bridge asleep", "open the mixer")
    code = (
        "from fl_mcp.errors import ToolError\n"
        "try:\n"
        "    fl.track('Kick').write([Note(key=36, start=0, length=1)])\n"
        "except ToolError as exc:\n"
        "    caught = (exc.category, exc.message)\n"
        "caught"
    )
    assert host.run(code).result == "('note_plane_unavailable', 'bridge asleep')"


def test_renders_land_in_the_read_only_renders_folder(host, workspace):
    """The server writes renders; the kernel names only the file, so it cannot aim a server write."""
    result = host.run("path = fl.render('../../escape.wav')\nstr(path), path.read_bytes()[:4]")
    assert result.result == repr((str(workspace.renders / "escape.wav"), b"RIFF"))
    for bad in ("/", "notes.txt"):
        assert "use a plain file name" in host.run(f"fl.render({bad!r})").traceback


@pytest.mark.parametrize("name", ["NUL.wav", "com1.wav", "a:b.wav", "mix.txt", ".wav", "../x"])
def test_render_names_that_could_reach_a_device_or_stream_are_refused(workspace, name):
    with pytest.raises(ToolError, match="plain file name"):
        workspace.output_path(Path(name))


def test_a_plain_render_name_maps_into_renders(workspace):
    assert workspace.output_path(Path("sub/Mix 2.wav")) == workspace.renders / "Mix 2.wav"


def test_file_edit_crosses_the_wire_and_closes_with_the_cell(host, session):
    assert host.run("with fl.file_edit():\n    pass").outcome == "ok"
    assert session.file_edit_log == ["enter", "exit"]
    host.run(
        "left_open = fl.file_edit()\nleft_open.__enter__()"
    )  # still referenced, so only the server closes it
    assert session.file_edit_log == ["enter", "exit", "enter", "exit"]


def test_wire_methods_are_exactly_what_the_sdk_calls_on_its_session():
    """The RPC surface is the SDK's session surface: no more (attack surface), no less (breakage)."""
    used = {
        node.attr
        for node in ast.walk(ast.parse(SDK_SOURCE.read_text()))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_session"
    }
    assert used == wire.SESSION_METHODS | {"file_edit"}


def test_wire_rejects_a_call_outside_the_sdk_surface():
    with pytest.raises(ValidationError):
        wire.parse_from_kernel(b'{"op": "call", "id": 1, "method": "close", "args": {}}')


def test_wire_round_trips_opaque_effect_bytes():
    effects = (Effect(name="Fruity Parametric EQ 2", chunk=b"\xff\x00\x10"),)
    assert wire.decode_result("add_effect", wire.encode_result("add_effect", effects)) == effects


# -- review findings: the server never blocks, late signals never misfire ------------


def test_a_kernel_that_stops_reading_cannot_block_the_server(tmp_path, workspace):
    """A full input pipe would block a plain write forever; the write deadline loses the kernel."""
    channels = [Channel(index=i, name="x" * 60) for i in range(5000)]  # a reply far past a pipe buffer
    session = _Session(store=Store("k", root=tmp_path / "s"), channels=channels)

    class _Deaf:
        confined = False

        def __call__(self, argv, *, workspace, env):
            script = (
                "import sys, time\n"
                'print(\'{"op": "ready", "pid": 1}\', flush=True)\n'
                "sys.stdin.readline()\n"
                'print(\'{"op": "call", "id": 1, "method": "snapshot"}\', flush=True)\n'
                "time.sleep(60)\n"
            )
            return PosixLauncher()([sys.executable, "-c", script], workspace=workspace, env=env)

        def release(self, workspace):
            return None

    kernel = KernelHost(session, workspace, _Deaf(), os.environ, write_timeout=0.5)
    started = time.monotonic()
    result = kernel.run("x")
    assert result.outcome == "kernel_lost"
    assert "stopped reading its input" in result.traceback
    assert time.monotonic() - started < 10


def test_fl_calls_from_many_threads_each_get_their_own_reply(host):
    code = (
        "import threading\n"
        "seen = []\n"
        "def work():\n"
        "    for _ in range(15):\n"
        "        seen.append((fl.tempo, len(fl.channels)))\n"
        "threads = [threading.Thread(target=work) for _ in range(4)]\n"
        "[t.start() for t in threads]\n"
        "[t.join(10) for t in threads]\n"
        "len(seen), set(seen)"
    )
    assert host.run(code).result == "(60, {(120.0, 3)})"


def test_an_fl_call_from_a_background_thread_between_cells_is_served(host):
    """It must not steal the next cell, nor the next cell its reply."""
    host.run(
        "import threading, time\nbox = []\nt = threading.Thread(target=lambda: (time.sleep(0.2), box.append(fl.tempo)))\nt.start()"
    )
    time.sleep(0.5)
    assert host.run("21 * 2").result == "42"
    assert host.run("t.join(5)\nbox").result == "[120.0]"


def test_a_late_interrupt_between_cells_is_ignored(host):
    """An interrupt that lands after its cell finished must not hit the next cell or the kernel loop."""
    host.run("kept = 'yes'")
    assert host._kernel.send(wire.Interrupt(), 1.0)
    time.sleep(0.3)
    assert host.run("import time\nfor _ in range(5):\n    time.sleep(0.05)\nkept").result == "'yes'"


def test_every_left_open_file_edit_closes_even_when_one_fails(host, session):
    session.file_edit_exit_failures = 1
    result = host.run("a, b = fl.file_edit(), fl.file_edit()\na.__enter__()\nb.__enter__()\n'done'")
    assert session.file_edit_log == ["enter", "enter", "exit", "exit"]
    assert result.outcome == "error"
    assert "FL did not come back" in result.traceback
    assert host.run("1 + 1").result == "2"  # the kernel is unaffected


def test_a_kernel_that_died_between_cells_is_replaced_and_the_cell_runs(host):
    host.run("import os, threading\nkept = 1\nthreading.Timer(0.1, os._exit, (3,)).start()")
    time.sleep(0.6)
    result = host.run("'kept' in globals()")
    assert result.outcome == "ok"
    assert result.result == "False"
    assert "exited with code 3 between runs" in result.stdout


def test_huge_output_is_truncated_in_the_kernel_not_fatal(host):
    result = host.run("x = 1\nprint('y' * 3_000_000)")
    assert result.outcome == "ok"
    assert "more characters not shown" in result.stdout
    assert len(result.stdout) < 1_100_000
    assert host.run("x").result == "1"


# -- real-Windows findings (dev box, 2026-10-18) ----------------------------------------


def test_stdin_is_an_empty_pipe_not_the_null_device(host):
    """An AppContainer is denied NUL; the kernel never opens a device for fd 0."""
    result = host.run("import os, stat, sys\nstat.S_ISFIFO(os.fstat(0).st_mode), sys.stdin.read()")
    assert result.result == "(True, '')"


def test_private_mkdir_inherits_instead(tmp_path):
    """A 0o700 mkdir on Windows is SYSTEM/Administrators/owner only: unusable in the container."""
    calls = []
    fake_os = SimpleNamespace(
        mkdir=lambda path, mode=0o777, *, dir_fd=None: calls.append((path, mode, dir_fd))
    )
    share_private_dirs(fake_os)
    fake_os.mkdir("private", 0o700)
    fake_os.mkdir("shared", 0o755, dir_fd=3)
    fake_os.mkdir("default")
    assert calls == [("private", 0o777, None), ("shared", 0o755, 3), ("default", 0o777, None)]


def test_every_python_the_kernel_starts_gets_the_private_mkdir_fix_on_windows(workspace):
    """pip and !python are separate interpreters; the kernel's PYTHONPATH sitecustomize patches them."""
    env = workspace.kernel_env(os.environ, python=Path(sys.executable))
    assert env["PYTHONPATH"] == str(KERNEL_SITE)
    site = KERNEL_SITE / "sitecustomize.py"
    probe = (
        "import os, sys\n"
        "import fl_mcp.kernel.private_dirs\n"  # imported before faking the platform
        "sys.platform = 'win32'\n"
        f"exec(compile(open({str(site)!r}).read(), 'sitecustomize', 'exec'), {{}})\n"
        "print(os.mkdir.__qualname__)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "share_private_dirs.<locals>.mkdir"
