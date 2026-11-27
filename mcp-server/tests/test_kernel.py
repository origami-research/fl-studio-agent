"""The run kernel: a real child process driven through KernelHost.

Every test here boots an actual ``python -m fl_mcp.kernel.child`` against the
sdk_fakes session, so persistence, isolation, timeouts, and the fl wire are
the shipped code paths. Network is never touched except by the opt-in test.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sdk_fakes import DEFAULT_CHANNELS, FakeSession, started_ahead

from fl_mcp import budget, errors
from fl_mcp.errors import ToolError
from fl_mcp.kernel import wire
from fl_mcp.kernel.cell import expand_magics, new_namespace, run_cell
from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import KERNEL_SITE, PosixLauncher, Workspace
from fl_mcp.kernel.null_device import share_null_device
from fl_mcp.kernel.private_dirs import share_private_dirs
from fl_mcp.schemas import Channel, RenderReceipt, Target
from fl_mcp.store import Store

SDK_SOURCE = Path(__file__).resolve().parents[1] / "src" / "fl_mcp" / "sdk.py"


class _Session(FakeSession):
    """The sdk_fakes session plus the seams these tests observe."""

    def __init__(self, *args, render_seconds: float = 0.0, tempo_seconds: float = 0.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.render_seconds = render_seconds
        self.tempo_seconds = tempo_seconds

    def render_wav(self, output: Path, *args, **kwargs) -> RenderReceipt:
        # As the real render: it knows how long it plays, and asks the run first.
        budget.require(self.render_seconds, "This render", "render fewer bars")
        time.sleep(self.render_seconds)
        return super().render_wav(output, *args, **kwargs)

    def set_tempo(self, bpm: float) -> float:
        # A call that does not play in real time but can still be slow (a preset load).
        time.sleep(self.tempo_seconds)
        return super().set_tempo(bpm)

@pytest.fixture
def session(tmp_path) -> _Session:
    return _Session(store=Store("kernel", root=tmp_path / "store"), channels=DEFAULT_CHANNELS)


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace.create(tmp_path / "kernel")


def _host(session, workspace, environ=None, **budgets) -> KernelHost:
    environ = os.environ if environ is None else environ
    return started_ahead(KernelHost(session, workspace, PosixLauncher(), environ, **budgets))


@pytest.fixture
def host(session, workspace) -> Iterator[KernelHost]:
    # Room for a fresh kernel's start inside a run, after a test loses the first one.
    kernel = _host(session, workspace, run_budget=30.0, interrupt_grace=1.0)
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
    kernel = _host(session, workspace, run_budget=120.0)
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
    kernel = _host(session, workspace, run_budget=0.5, interrupt_grace=2.0)
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
    kernel = _host(session, workspace, run_budget=0.3, interrupt_grace=0.3)
    try:
        kernel.run("kept = 'yes'")
        lost = kernel.run("import time\ntime.sleep(30)")
        assert lost.outcome == "kernel_lost"
        assert "did not stop" in lost.traceback
        started_ahead(kernel)  # the next start, outside the 0.3 s budget
        assert "NameError" in kernel.run("kept").traceback
        assert kernel.run("1 + 1").result == "2"
    finally:
        kernel.close()


# -- the run budget counts fl calls (#333) ---------------------------------------------


def test_time_inside_fl_calls_counts_and_the_cell_stops_between_them(tmp_path, workspace):
    """#333: a cell of slow fl calls outlasted the harness's 900 s because only kernel
    compute was budgeted. Without the fix this loop never ends."""
    slow = _Session(store=Store("k", root=tmp_path / "s"), channels=DEFAULT_CHANNELS, tempo_seconds=0.3)
    kernel = _host(slow, workspace, run_budget=1.0, interrupt_grace=2.0)
    try:
        kernel.run("kept = 'yes'")
        started = time.monotonic()
        result = kernel.run("while True:\n    fl.set_tempo(100)")
        took = time.monotonic() - started
        assert result.outcome == "interrupted", result
        assert "1 s budget (time in fl calls included)" in result.traceback
        assert result.actions and result.actions[0].startswith("Set tempo to 100 BPM")
        assert took < 1.0 + 0.3 + 1.0  # the budget, the call in flight, and slack
        assert kernel.run("kept").result == "'yes'"
    finally:
        kernel.close()


def test_no_fl_call_is_served_once_the_budget_is_spent(tmp_path, workspace):
    """The call in flight finishes; the next one never reaches FL, whether the interrupt
    stops the cell before it is sent or the server refuses it."""
    slow = _Session(store=Store("k", root=tmp_path / "s"), channels=DEFAULT_CHANNELS, tempo_seconds=2.0)
    kernel = _host(slow, workspace, run_budget=1.0, interrupt_grace=3.0)
    try:
        result = kernel.run("try:\n    fl.set_tempo(100)\nfinally:\n    fl.set_tempo(90)")
        assert result.outcome == "interrupted", result
        assert result.actions == ["Set tempo to 100 BPM"]
        assert slow.tempo() == 100
    finally:
        kernel.close()


def test_an_interrupt_sent_before_its_cell_starts_stops_that_cell_and_no_other():
    """Once a run's budget is gone the server interrupts right after sending the cell, and
    the interrupt can reach the kernel before the cell starts. It used to be dropped, so
    the cell ran unbounded; an interrupt for a cell that already finished still must not
    touch the next one. Run in its own process: Channel takes over SIGINT and the wire."""
    script = textwrap.dedent(
        """
        import os, time
        from fl_mcp.kernel import wire
        from fl_mcp.kernel.child import Channel

        inbound, feed = os.pipe()
        _, outbound = os.pipe()
        channel = Channel(os.fdopen(inbound, "rb"), os.fdopen(outbound, "wb"))
        feed = os.fdopen(feed, "wb")

        def send(message):
            feed.write(wire.encode(message))
            feed.flush()

        namespace = {}
        send(wire.Exec(code="ran = 1"))
        send(wire.Interrupt())
        end = time.monotonic() + 10
        while channel._interrupted != 1 and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.2)  # the handler runs now, with no cell running, and does nothing
        first = channel.run(*channel._execs.get(), namespace)
        send(wire.Interrupt())  # for the cell that just finished
        send(wire.Exec(code="ran = 2"))
        number, cell = channel._execs.get()
        time.sleep(0.2)
        second = channel.run(number, cell, namespace)
        print(first.traceback.strip(), second.outcome, namespace.get("ran"), flush=True)
        os._exit(0)
        """
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert done.stdout.split() == ["KeyboardInterrupt", "ok", "2"], done.stderr


def test_a_render_that_would_not_fit_is_refused_before_it_starts(tmp_path, workspace):
    """The server tells the session how long the run has left while it serves a call."""
    slow = _Session(store=Store("k", root=tmp_path / "s"), channels=DEFAULT_CHANNELS, render_seconds=30.0)
    kernel = _host(slow, workspace, run_budget=5.0, interrupt_grace=1.0)
    try:
        started = time.monotonic()
        result = kernel.run("fl.render('mix.wav')")
        assert time.monotonic() - started < 5.0
        assert result.outcome == "error" and result.actions == []
        assert "takes about 30 s, but this run has" in result.traceback
        assert "left of its 5 s budget" in result.traceback
        assert slow.renders == []
    finally:
        kernel.close()


def test_a_run_that_cannot_get_the_kernel_within_its_budget_is_refused(session, workspace):
    """A run waiting behind another must not outlast its own budget either."""
    kernel = _host(session, workspace, run_budget=1.0, interrupt_grace=3.0)
    slow = (
        "import time\n"
        "try:\n"
        "    while True:\n"
        "        pass\n"
        "except KeyboardInterrupt:\n"
        "    end = time.monotonic() + 1.5\n"
        "    while time.monotonic() < end:\n"
        "        pass\n"
    )
    try:
        assert kernel.run("0").result == "0"  # the start ahead no longer holds the lock
        first = threading.Thread(target=kernel.run, args=(slow,))
        first.start()
        _wait_for(kernel._lock.locked)
        with pytest.raises(ToolError) as refused:
            kernel.run("1")
        assert refused.value.category == "run_budget_exceeded"
        assert refused.value.message.startswith("This code did not run")
        first.join(10)
        assert kernel.run("1 + 1").result == "2"
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


class _Counting(PosixLauncher):
    """Counts launches; ``script`` replaces the kernel when given."""

    def __init__(self, script: str | None = None) -> None:
        super().__init__()
        self.launches = 0
        self.script = script

    def __call__(self, argv, *, workspace, env):
        self.launches += 1
        if self.script is not None:
            argv = [sys.executable, "-c", self.script]
        return super().__call__(argv, workspace=workspace, env=env)


def _wait_for(condition, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_a_kernel_started_ahead_runs_the_first_cell(session, workspace):
    """#324: a cold box took longer than the start limit, so the server starts the kernel
    when it starts; the first run uses that kernel instead of starting another."""
    launcher = _Counting()
    kernel = KernelHost(session, workspace, launcher, os.environ)
    try:
        kernel.start()
        assert kernel.run("1 + 1").result == "2"  # waits for the start under way, if it is first
        _wait_for(lambda: kernel._lock.acquire(blocking=False))  # the start ahead has finished either way
        kernel._lock.release()
        assert launcher.launches == 1
    finally:
        kernel.close()


def test_a_failed_start_ahead_is_the_first_runs_error_and_the_next_run_starts_again(session, workspace):
    """The first run reports why the kernel could not start ahead, without a second wait."""
    launcher = _Counting("import sys; sys.stderr.write('boom on import\\n'); sys.exit(2)")
    kernel = KernelHost(session, workspace, launcher, os.environ)
    kernel.start()
    _wait_for(lambda: kernel._start_failure is not None)
    with pytest.raises(ToolError) as first:
        kernel.run("1")
    assert "exited with code 2" in first.value.message and "boom on import" in first.value.message
    assert launcher.launches == 1
    with pytest.raises(ToolError):
        kernel.run("1")
    assert launcher.launches == 2


def test_close_kills_a_kernel_that_is_still_starting(session, workspace):
    """Shutdown must not wait for a start, nor leave its process behind."""
    launcher = _Counting("import time; time.sleep(60)")
    kernel = KernelHost(session, workspace, launcher, os.environ)
    kernel.start()
    _wait_for(lambda: kernel._starting is not None)
    starting = kernel._starting
    kernel.close()
    _wait_for(lambda: starting.process.poll() is not None, seconds=10)
    _wait_for(lambda: kernel._start_failure is not None, seconds=10)
    assert kernel._kernel is None


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
    for finding in ("canary_readable=True", "outside_writable=True", "home_readable=True", "loopback_reachable=True"):
        assert finding in caught.value.message
    assert not list(workspace.root.glob("probe-write-*"))  # the probe cleans up what it could create


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


def test_an_uncaught_tool_error_shows_its_fix_in_the_traceback(host, session):
    """The traceback is all a model reads of a failed cell, so the fix must be in it.

    A live run on Windows read the controller refusal's message but never its fix
    (rerun fl-mcp-install): the traceback printed only the message."""
    refusal = errors.controller_mismatch("0.2.8", "0.2.10")
    session.fail_next_write = refusal
    result = host.run("fl.track('Kick').write([Note(key=36, start=0, length=1)])")
    assert result.outcome == "error"
    assert result.traceback.endswith(f"ToolError: {refusal.message}\nFix: {refusal.fix}\n"), result.traceback


@pytest.mark.parametrize("fix", ["", "No pattern 'x'."])
def test_a_tool_error_without_its_own_fix_shows_the_message_once(fix):
    """No empty "Fix:" line, and no fix that only repeats the message."""
    error = ToolError("unknown_target", "No pattern 'x'.", fix)
    result = run_cell("raise error", {**new_namespace(None), "error": error})
    assert result.traceback.endswith("ToolError: No pattern 'x'.\n"), result.traceback


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


def test_wire_methods_are_exactly_what_the_sdk_calls_on_its_session():
    """The RPC surface is the SDK's session surface: no more (attack surface), no less (breakage)."""
    used = {
        node.attr
        for node in ast.walk(ast.parse(SDK_SOURCE.read_text()))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_session"
    }
    assert used == wire.SESSION_METHODS


def test_wire_rejects_a_call_outside_the_sdk_surface():
    with pytest.raises(ValidationError):
        wire.parse_from_kernel(b'{"op": "call", "id": 1, "method": "close", "args": {}}')


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
                'print(\'{"op": "call", "id": 1, "method": "live_channels"}\', flush=True)\n'
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


def test_the_null_device_stand_in_is_a_workspace_file_and_a_fresh_one_per_process(tmp_path):
    """An AppContainer is denied NUL (box run 2026-11-06): os.devnull and subprocess.DEVNULL
    become workspace files, a new self-deleting one for each process started."""
    opened = []
    fake_os = SimpleNamespace(
        path=os.path, makedirs=lambda path, exist_ok: opened.append(("makedirs", path)),
        open=lambda path, flags: opened.append((path, flags)) or len(opened),
        O_RDWR=1, O_CREAT=2, O_EXCL=4, O_BINARY=8, O_TEMPORARY=16, O_NOINHERIT=32,
    )
    fake_subprocess = SimpleNamespace(Popen=type("Popen", (), {}))
    share_null_device(fake_os, fake_subprocess, str(tmp_path))

    assert fake_os.devnull == str(tmp_path / "devnull")
    first, second = fake_subprocess.Popen(), fake_subprocess.Popen()
    assert first._get_devnull() == first._get_devnull()  # one file for all three streams of a process
    second._get_devnull()
    files = [call for call in opened if call[0] != "makedirs"]
    assert len(files) == 2 and files[0][0] != files[1][0]
    assert all(Path(path).parent == tmp_path and flags == 63 for path, flags in files)


def test_every_python_the_kernel_starts_gets_the_windows_fixes(workspace):
    """pip and !python are separate interpreters; the kernel's PYTHONPATH sitecustomize patches them."""
    env = workspace.kernel_env(os.environ, python=Path(sys.executable))
    assert env["PYTHONPATH"] == str(KERNEL_SITE)
    site = KERNEL_SITE / "sitecustomize.py"
    probe = (
        "import os, subprocess, sys\n"
        "import fl_mcp.kernel.private_dirs, fl_mcp.kernel.null_device\n"  # imported before faking the platform
        "sys.platform = 'win32'\n"
        f"exec(compile(open({str(site)!r}).read(), 'sitecustomize', 'exec'), {{}})\n"
        "print(os.mkdir.__qualname__, subprocess.Popen._get_devnull.__qualname__, os.devnull == os.path.join(os.environ['TMP'], 'devnull'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, env=dict(os.environ, TMP=str(workspace.tmp))
    )
    assert out.stdout.split() == ["share_private_dirs.<locals>.mkdir", "share_null_device.<locals>._get_devnull", "True"]


def test_the_stand_in_replaces_the_one_subprocess_method_that_opens_the_null_device():
    """CPython's Popen opens os.devnull in _get_devnull only; a Python that renamed it would
    leave DEVNULL on the denied device, so this fails first."""
    assert "os.open(os.devnull" in inspect.getsource(subprocess.Popen._get_devnull)


# -- a kernel without FL ------------------------------------------------------


def test_a_kernel_without_an_fl_session_runs_python_and_refuses_fl(workspace):
    """The box lockdown probes the boundary with a kernel that has no FL session.

    It crashed every cell ("'NoneType' object has no attribute 'track_name'")
    once tool results started listing actions, so the lockdown could not be
    verified on a box (2026-10-20 dev rehearsal)."""
    kernel = _host(None, workspace, run_budget=5.0, interrupt_grace=1.0)
    try:
        plain = kernel.run("1 + 1")
        assert plain.outcome == "ok" and plain.actions == []
        assert "2" in (plain.result or plain.stdout)
        refused = kernel.run("fl.tempo")
        assert refused.outcome == "error" and refused.actions == []
        assert "not connected to FL Studio" in (refused.traceback or "")
    finally:
        kernel.close()


def test_a_kernel_without_an_fl_session_still_opens_the_guides(workspace):
    """Computer-use runs (--workspace-only) have no FL session, and their description
    and the harness point to fl.guide() (fl-studio-cua#27): it reads only package files."""
    kernel = _host(None, workspace, run_budget=10.0, interrupt_grace=1.0)
    try:
        listed = kernel.run("fl.guide()")
        assert listed.outcome == "ok", listed.traceback
        assert "sidechain" in listed.stdout and "listening" in listed.stdout
        one = kernel.run('fl.guide("sidechain")')
        assert one.outcome == "ok", one.traceback
        assert "fl.automate" in one.stdout
    finally:
        kernel.close()
