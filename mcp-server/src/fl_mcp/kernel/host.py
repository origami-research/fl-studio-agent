"""The server side of the run kernel: boot it, run cells, serve its fl calls.

The MCP server owns the FL session; the kernel only ever reaches it through
``KernelHost._serve_call``. A cell that hangs is interrupted, a kernel that
ignores the interrupt or dies is killed and replaced on the next run, and
every one of those endings is a distinct RunResult outcome.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from pydantic import ValidationError

from fl_mcp import errors
from fl_mcp.contract import RunResult
from fl_mcp.errors import ToolError
from fl_mcp.kernel import wire
from fl_mcp.kernel.launch import KernelProcess, Launcher, Workspace
from fl_mcp.session import FLSession

log = logging.getLogger(__name__)

# Kernel compute per cell. Time the server spends serving the cell's fl calls
# (a render plays the whole arrangement) is not charged against it.
RUN_CELL_TIMEOUT_SECONDS = 300.0
# How long an interrupted cell gets to unwind before the kernel is killed.
INTERRUPT_GRACE_SECONDS = 5.0
# Interpreter start plus the SDK import; generous for a cold Windows box.
KERNEL_BOOT_TIMEOUT_SECONDS = 60.0
# A kernel that stops reading its input must not block the server: a write to
# it that cannot finish in this long loses the kernel.
WIRE_WRITE_TIMEOUT_SECONDS = 10.0
_STDERR_TAIL_LINES = 20
_STDERR_LINE_BYTES = 4096
# Kernel stderr reaches the server log only up to this many lines per kernel,
# so a print loop to fd 2 cannot fill the disk; the tail is always kept.
_STDERR_LOG_LINES = 200
# The server reads kernel output into memory; a line past this is an attack or
# a runaway print, never a legitimate SDK call or cell result.
_MAX_MESSAGE_BYTES = 64 << 20
# A dead kernel's stderr pipe closes at once unless a grandchild still holds it.
_STDERR_DRAIN_SECONDS = 2.0
# Session calls whose ``output`` path the server writes: confined to renders.
_SERVER_WRITES = frozenset({"render_wav", "export_offline"})


class _Exited:
    """The kernel closed its end of the wire: the process is gone or going."""


class _Garbled:
    def __init__(self, detail: str) -> None:
        self.detail = detail


class _Kernel:
    """One live kernel process and the threads that drain its pipes."""

    def __init__(self, process: KernelProcess) -> None:
        self.process = process
        self.messages: queue.Queue[wire.Ready | wire.Call | wire.Done | _Exited | _Garbled] = queue.Queue()
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
        threading.Thread(target=self._read_wire, args=(process.stdout,), daemon=True).start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, args=(process.stderr,), daemon=True)
        self._stderr_reader.start()

    def _read_wire(self, stream: IO[bytes]) -> None:
        while line := stream.readline(_MAX_MESSAGE_BYTES + 1):
            if len(line) > _MAX_MESSAGE_BYTES:
                self.messages.put(_Garbled(f"sent a message over {_MAX_MESSAGE_BYTES >> 20} MiB"))
                return
            try:
                self.messages.put(wire.parse_from_kernel(line))
            except ValidationError as exc:
                self.messages.put(_Garbled(f"sent an unreadable message ({exc.error_count()} errors)"))
                return
        self.messages.put(_Exited())

    def _read_stderr(self, stream: IO[bytes]) -> None:
        logged = 0
        while raw := stream.readline(_STDERR_LINE_BYTES):
            line = raw.decode("utf-8", "replace").rstrip()
            self.stderr_tail.append(line)
            if logged < _STDERR_LOG_LINES:
                log.info("kernel %d: %s", self.process.pid, line)
            elif logged == _STDERR_LOG_LINES:
                log.warning("kernel %d: further stderr is not logged", self.process.pid)
            logged += 1

    def next(self, timeout: float) -> wire.Ready | wire.Call | wire.Done | _Exited | _Garbled | None:
        try:
            return self.messages.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def send(self, message: wire.Exec | wire.Interrupt | wire.Reply, timeout: float) -> bool:
        """False when the write did not finish in ``timeout``: the kernel stopped reading.

        The write runs on its own thread so a full pipe cannot block the server;
        killing the kernel unblocks it. A send to a dead kernel is dropped and
        its reader reports the exit.
        """
        data = wire.encode(message)
        finished = threading.Event()

        def write() -> None:
            with contextlib.suppress(OSError, ValueError):
                self.process.stdin.write(data)
                self.process.stdin.flush()
            finished.set()

        threading.Thread(target=write, name="kernel-write", daemon=True).start()
        return finished.wait(timeout)

    def kill(self) -> int:
        """Kill and reap; the stderr tail is complete once this returns."""
        self.process.kill()
        code = self.process.wait()
        self._stderr_reader.join(timeout=_STDERR_DRAIN_SECONDS)
        return code

    def stderr_note(self) -> str:
        return "\nKernel stderr (last lines):\n" + "\n".join(self.stderr_tail) if self.stderr_tail else ""


class _FileEdits:
    """The session file_edit contexts a cell opened; all closed when the cell ends."""

    def __init__(self, session: FLSession) -> None:
        self._session = session
        self._open: list[contextlib.ExitStack] = []

    def enter(self) -> None:
        stack = contextlib.ExitStack()
        stack.enter_context(self._session.file_edit())
        self._open.append(stack)

    def exit(self) -> None:
        if not self._open:
            raise errors.invalid_argument("fl.file_edit", "exit without a matching enter")
        self._open.pop().close()

    def close(self) -> str | None:
        """Close every context, even past a failure; return what failed, if anything."""
        failures = []
        while self._open:
            try:
                self._open.pop().close()
            except Exception as exc:  # the rest must still close; the cell result reports it
                log.exception("closing a file_edit a cell left open failed")
                failures.append(f"{type(exc).__name__}: {exc}")
        return "; ".join(failures) or None


class KernelHost:
    """Runs cells in one persistent kernel, restarting it after it is lost."""

    def __init__(
        self,
        session: FLSession,
        workspace: Workspace,
        launcher: Launcher,
        environ: Mapping[str, str],
        *,
        cell_timeout: float = RUN_CELL_TIMEOUT_SECONDS,
        interrupt_grace: float = INTERRUPT_GRACE_SECONDS,
        boot_timeout: float = KERNEL_BOOT_TIMEOUT_SECONDS,
        write_timeout: float = WIRE_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace = workspace
        self._session = session
        self._launcher = launcher
        self._environ = environ
        self._cell_timeout = cell_timeout
        self._interrupt_grace = interrupt_grace
        self._boot_timeout = boot_timeout
        self._write_timeout = write_timeout
        self._kernel: _Kernel | None = None
        self._lock = threading.Lock()  # MCP runs sync tools on worker threads

    def run(self, code: str) -> RunResult:
        """Run one cell. Raises kernel_unavailable if no kernel can start."""
        with self._lock:
            note = self._drop_dead_kernel()
            if self._kernel is None:
                self._kernel = self._boot()
            kernel = self._kernel
            try:
                result = self._run_cell(kernel, code)
            except BaseException:
                self._kernel = None
                kernel.kill()
                raise
            if result.outcome == "kernel_lost":
                self._kernel = None
            return result.model_copy(update={"stdout": note + result.stdout}) if note else result

    def close(self) -> None:
        """Kill the kernel without waiting for a running cell: shutdown must not hang on one."""
        kernel, self._kernel = self._kernel, None
        if kernel is not None:
            kernel.kill()
        self._launcher.release(self.workspace)

    # -- lifecycle ----------------------------------------------------------------

    def _drop_dead_kernel(self) -> str:
        """A kernel can die between cells (a background thread's os._exit); say so, start fresh."""
        if self._kernel is None or self._kernel.process.poll() is None:
            return ""
        code = self._kernel.kill()
        self._kernel = None
        return (
            f"[The previous kernel exited with code {code} between runs; its variables are gone. "
            "This cell ran in a fresh kernel.]\n"
        )

    def _boot(self) -> _Kernel:
        argv = [sys.executable, "-I", "-m", "fl_mcp.kernel.child"]
        if self._launcher.confined:
            argv += ["--probe", str(self.workspace.canary)]
        env = self.workspace.kernel_env(self._environ, python=Path(sys.executable))
        try:
            kernel = _Kernel(self._launcher(argv, workspace=self.workspace, env=env))
        except (OSError, subprocess.SubprocessError) as exc:
            log.exception("kernel launch failed")
            raise errors.kernel_unavailable(f"launch failed: {exc}") from exc
        message = kernel.next(self._boot_timeout)
        if not isinstance(message, wire.Ready):
            code = kernel.kill()
            reason = "did not start in time" if message is None else f"exited with code {code}"
            raise errors.kernel_unavailable(f"the kernel {reason}.{kernel.stderr_note()}")
        if self._launcher.confined and (message.probe is None or _breached(message.probe)):
            kernel.kill()
            log.error("kernel boundary probe failed: %s", message.probe)
            raise errors.kernel_unavailable(
                f"its OS boundary is not in place ({message.probe}); refusing to run model code unconfined."
            )
        log.info("kernel %d ready in %s", message.pid, self.workspace.work)
        return kernel

    def _run_cell(self, kernel: _Kernel, code: str) -> RunResult:
        edits = _FileEdits(self._session)
        try:
            result = self._cell_outcome(kernel, code, edits)
        finally:
            failure = edits.close()
        if failure is None:
            return result
        note = f"Closing the fl.file_edit block this cell left open failed: {failure}"
        if result.outcome == "ok":
            return RunResult(outcome="error", stdout=result.stdout, traceback=note)
        return result.model_copy(update={"traceback": f"{result.traceback}\n{note}"})

    def _cell_outcome(self, kernel: _Kernel, code: str, edits: _FileEdits) -> RunResult:
        if not kernel.send(wire.Exec(code=code), self._write_timeout):
            return self._stalled(kernel)
        result = self._await_done(kernel, edits, self._cell_timeout)
        if result is not None:
            return result
        if not kernel.send(wire.Interrupt(), self._write_timeout):
            return self._stalled(kernel)
        result = self._await_done(kernel, edits, self._interrupt_grace)
        if result is None:
            return self._lost(
                kernel,
                f"ran past the {self._cell_timeout:g} s cell budget and did not stop within "
                f"{self._interrupt_grace:g} s of an interrupt, so it was killed",
            )
        if result.outcome != "error":
            return result
        return result.model_copy(
            update={
                "outcome": "interrupted",
                "traceback": f"The cell ran past the {self._cell_timeout:g} s budget and was "
                "interrupted; the kernel and its variables are intact.\n" + (result.traceback or ""),
            }
        )

    def _await_done(self, kernel: _Kernel, edits: _FileEdits, budget: float) -> RunResult | None:
        """Serve the cell's calls until it finishes; None when ``budget`` runs out."""
        remaining = budget
        while True:
            started = time.monotonic()
            message = kernel.next(remaining)
            remaining -= time.monotonic() - started
            if message is None:
                return None
            if isinstance(message, wire.Done):
                return message.result
            if isinstance(message, wire.Call):
                if not kernel.send(self._serve_call(message, edits), self._write_timeout):
                    return self._stalled(kernel)
            elif isinstance(message, _Garbled):
                return self._lost(kernel, f"{message.detail}, so it was killed")
            else:
                code = kernel.kill()
                return self._lost(kernel, f"exited with code {code}")

    def _stalled(self, kernel: _Kernel) -> RunResult:
        return self._lost(
            kernel, f"stopped reading its input for {self._write_timeout:g} s, so it was killed"
        )

    def _lost(self, kernel: _Kernel, what_happened: str) -> RunResult:
        kernel.kill()
        return RunResult(
            outcome="kernel_lost",
            stdout="",
            traceback=f"The kernel process {what_happened}. Its variables and imports are gone and "
            "this cell's output was lost; files in the workspace remain. The next run starts a "
            f"fresh kernel.{kernel.stderr_note()}",
        )

    # -- the fl surface ---------------------------------------------------------------

    def _serve_call(self, call: wire.Call, edits: _FileEdits) -> wire.Reply:
        try:
            value = self._dispatch(call, edits)
        except ToolError as exc:
            return wire.Reply(id=call.id, error=wire.WireError.from_tool_error(exc))
        except Exception as exc:  # the RPC boundary: logged here, reported to the cell as its error
            log.exception("kernel call %s failed", call.method)
            return wire.Reply(
                id=call.id,
                error=wire.WireError(
                    category="internal_error",
                    message=f"{type(exc).__name__}: {exc}",
                    fix="Report this; the engine kept running.",
                ),
            )
        return wire.Reply(id=call.id, value=value)

    def _dispatch(self, call: wire.Call, edits: _FileEdits) -> Any:
        if call.method == wire.FILE_EDIT_ENTER:
            return edits.enter()
        if call.method == wire.FILE_EDIT_EXIT:
            return edits.exit()
        kwargs = wire.decode_call(call)
        if call.method in _SERVER_WRITES:
            kwargs["output"] = self.workspace.output_path(kwargs["output"])
        return wire.encode_result(call.method, getattr(self._session, call.method)(**kwargs))


def _breached(probe: wire.BoundaryProbe) -> bool:
    return probe.canary_readable or probe.imds_reachable
