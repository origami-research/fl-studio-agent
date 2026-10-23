"""The kernel process: ``python -I -m fl_mcp.kernel.child [--probe CANARY]``.

It owns one persistent namespace and runs cells the server sends. ``fl`` is the
real SDK (sdk.FL) over a session whose every call crosses the wire to the
server, which owns the actual FL session. The wire is the stdin/stdout pipe
pair the server created; at startup the kernel moves it to private
descriptors and points fd 0 at an empty pipe and fd 1 at stderr, so neither
``input()`` nor a stray ``os.write(1, ...)`` from model code can touch it.
"""

from __future__ import annotations

import _thread
import argparse
import contextlib
import faulthandler
import functools
import importlib
import itertools
import os
import queue
import signal
import sys
import threading
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

from pydantic import ValidationError

from fl_mcp import sdk
from fl_mcp.contract import RunResult
from fl_mcp.kernel import wire
from fl_mcp.kernel.cell import new_namespace, run_cell
from fl_mcp.kernel.private_dirs import share_private_dirs
from fl_mcp.kernel.probe import probe

# Exit status when the server sends something the kernel cannot parse.
EXIT_WIRE_ERROR = 70
# How often a waiting fl call lets a pending interrupt land.
_CALL_POLL_SECONDS = 0.1


class Channel:
    """The kernel's end of the wire.

    A reader thread routes each reply to the one call waiting on its id (any
    thread may call fl), queues cells separately, and forwards interrupts.
    An interrupt counts only while a cell runs: ``_on_interrupt`` is the SIGINT
    handler, so one that lands after its cell finished is dropped instead of
    killing the next cell or the kernel's own loop.
    """

    def __init__(self, inbound: IO[bytes], outbound: IO[bytes]) -> None:
        self._outbound = outbound
        self._send_lock = threading.Lock()
        self._execs: queue.SimpleQueue[wire.Exec] = queue.SimpleQueue()
        self._pending: dict[int, queue.SimpleQueue[wire.Reply]] = {}
        self._pending_lock = threading.Lock()
        self._call_ids = itertools.count(1)
        self._cell_running = False
        signal.signal(signal.SIGINT, self._on_interrupt)
        threading.Thread(target=self._read, args=(inbound,), name="kernel-wire", daemon=True).start()

    def _read(self, inbound: IO[bytes]) -> None:
        for line in inbound:
            try:
                message = wire.parse_to_kernel(line)
            except ValidationError:
                traceback.print_exc()
                os._exit(EXIT_WIRE_ERROR)
            if isinstance(message, wire.Interrupt):
                _thread.interrupt_main()
            elif isinstance(message, wire.Exec):
                self._execs.put(message)
            else:
                with self._pending_lock:
                    waiter = self._pending.get(message.id)
                if waiter is not None:  # else: the late reply to a call an interrupt abandoned
                    waiter.put(message)
        os._exit(0)  # the server closed the wire; nobody is left to answer

    def _on_interrupt(self, signum: int, frame: object) -> None:
        if self._cell_running:
            raise KeyboardInterrupt

    def send(self, message: wire.Ready | wire.Call | wire.Done) -> None:
        with self._send_lock:
            wire.send(self._outbound, message)

    def run(self, cell: wire.Exec, namespace: dict[str, Any]) -> RunResult:
        importlib.invalidate_caches()  # packages installed since the last cell import
        self._cell_running = True
        try:
            return run_cell(cell.code, namespace)
        except KeyboardInterrupt:  # landed in run_cell's own frame, after the user code
            return RunResult(outcome="error", stdout="", traceback="KeyboardInterrupt\n", actions=[])
        finally:
            self._cell_running = False

    def serve(self, namespace: dict[str, Any]) -> None:
        while True:
            cell = self._execs.get()
            self.send(wire.Done(result=self.run(cell, namespace)))

    def call(self, call: wire.Call) -> Any:
        call = call.model_copy(update={"id": next(self._call_ids)})
        waiter: queue.SimpleQueue[wire.Reply] = queue.SimpleQueue()
        with self._pending_lock:
            self._pending[call.id] = waiter
        try:
            self.send(call)
            while True:
                # A timed get lets an interrupt land between slices.
                with contextlib.suppress(queue.Empty):
                    reply = waiter.get(timeout=_CALL_POLL_SECONDS)
                    break
        finally:
            with self._pending_lock:
                del self._pending[call.id]
        if reply.error is not None:
            raise reply.error.to_tool_error()
        return reply.value


class RemoteSession:
    """Stands in for FLSession inside the kernel: each method is a wire call."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in wire.SESSION_METHODS:
            raise AttributeError(name)
        return functools.partial(self._call, name)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        value = self._channel.call(wire.encode_call(method, args, kwargs))
        return wire.decode_result(method, value)

    @contextlib.contextmanager
    def file_edit(self) -> Iterator[None]:
        self._channel.call(wire.Call(method=wire.FILE_EDIT_ENTER))
        try:
            yield
        finally:
            self._channel.call(wire.Call(method=wire.FILE_EDIT_EXIT))


def boundary_probe(canary: Path) -> wire.BoundaryProbe:
    """Try what the OS boundary must deny; the server refuses a kernel that succeeds."""
    return wire.BoundaryProbe.model_validate(probe(canary))


def _take_wire() -> tuple[IO[bytes], IO[bytes]]:
    """Move the wire off fds 0/1 so model code cannot read or corrupt it."""
    inbound = os.fdopen(os.dup(0), "rb")
    outbound = os.fdopen(os.dup(1), "wb")
    # fd 0 becomes a pipe whose write end is already closed: reads see EOF.
    # Not the null device, which an AppContainer is denied.
    empty, closed = os.pipe()
    os.close(closed)
    os.dup2(empty, 0)
    os.close(empty)
    os.dup2(2, 1)
    return inbound, outbound


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, help="canary file the boundary must hide")
    args = parser.parse_args(argv)
    faulthandler.enable()  # a native crash leaves its stack on stderr for the server's log
    if sys.platform == "win32":
        share_private_dirs(os)
    with contextlib.suppress(OSError):  # model code may have removed it; tempfile needs it
        Path(os.environ["TMP"]).mkdir(exist_ok=True)
    inbound, outbound = _take_wire()
    channel = Channel(inbound, outbound)
    # -I keeps the workspace off sys.path while the kernel itself imports;
    # model code then gets it, and the packages it installs, like a notebook.
    sys.path.extend([str(Path.cwd()), os.environ["PIP_TARGET"]])
    namespace = new_namespace(sdk.FL(RemoteSession(channel)))
    probe = boundary_probe(args.probe) if args.probe else None
    channel.send(wire.Ready(pid=os.getpid(), probe=probe))
    channel.serve(namespace)


if __name__ == "__main__":
    main()
