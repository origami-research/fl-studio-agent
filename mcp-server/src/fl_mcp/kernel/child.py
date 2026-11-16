"""The kernel process: ``python -I -m fl_mcp.kernel.child [--probe CANARY --probe-home HOME --probe-port PORT]``.

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
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from pydantic import ValidationError

from fl_mcp import sdk
from fl_mcp.contract import RunResult
from fl_mcp.kernel import wire
from fl_mcp.kernel.cell import new_namespace, run_cell
from fl_mcp.kernel.null_device import share_null_device
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
    An interrupt belongs to the cell the reader last received and counts only
    for that cell: one sent before its cell started (the run's budget was spent
    as the cell was sent) stops it as it starts, and one that lands after its
    cell finished is dropped instead of killing the next cell or the kernel's
    own loop. ``_on_interrupt`` is the SIGINT handler, and Python may run it
    late, once the main thread is already in the next cell, so it checks whose
    interrupt it is.
    """

    def __init__(self, inbound: IO[bytes], outbound: IO[bytes]) -> None:
        self._outbound = outbound
        self._send_lock = threading.Lock()
        self._execs: queue.SimpleQueue[tuple[int, wire.Exec]] = queue.SimpleQueue()
        self._pending: dict[int, queue.SimpleQueue[wire.Reply]] = {}
        self._pending_lock = threading.Lock()
        self._call_ids = itertools.count(1)
        # Cells by number: the last one received, the last one finished, the one running
        # (0 when none), and the one the last interrupt was for.
        self._received = self._finished = self._running = self._interrupted = 0
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
                if self._received > self._finished:  # else its cell already finished
                    self._interrupted = self._received
                    _thread.interrupt_main()
            elif isinstance(message, wire.Exec):
                self._received += 1
                self._execs.put((self._received, message))
            else:
                with self._pending_lock:
                    waiter = self._pending.get(message.id)
                if waiter is not None:  # else: the late reply to a call an interrupt abandoned
                    waiter.put(message)
        os._exit(0)  # the server closed the wire; nobody is left to answer

    def _on_interrupt(self, signum: int, frame: object) -> None:
        if self._running and self._running == self._interrupted:
            raise KeyboardInterrupt

    def send(self, message: wire.Ready | wire.Call | wire.Done) -> None:
        with self._send_lock:
            wire.send(self._outbound, message)

    def run(self, number: int, cell: wire.Exec, namespace: dict[str, Any]) -> RunResult:
        try:
            self._running = number
            if self._interrupted != number:  # else it came before the cell started
                importlib.invalidate_caches()  # packages installed since the last cell import
                return run_cell(cell.code, namespace)
        except KeyboardInterrupt:  # before the user code, or in run_cell's own frame after it
            pass
        finally:
            self._running = 0
            self._finished = number
        return RunResult(outcome="error", stdout="", traceback="KeyboardInterrupt\n", actions=[])

    def serve(self, namespace: dict[str, Any]) -> None:
        while True:
            number, cell = self._execs.get()
            self.send(wire.Done.of(self.run(number, cell, namespace)))

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


def boundary_probe(canary: Path, home: Path, loopback_port: int) -> wire.BoundaryProbe:
    """Try what the OS boundary must deny; the server refuses a kernel that succeeds."""
    return wire.BoundaryProbe.model_validate(probe(canary, home, loopback_port))


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
    parser.add_argument("--probe-home", type=Path, help="the user's home folder the boundary must hide")
    parser.add_argument("--probe-port", type=int, help="a loopback port the boundary must not reach")
    args = parser.parse_args(argv)
    faulthandler.enable()  # a native crash leaves its stack on stderr for the server's log
    if sys.platform == "win32":
        share_private_dirs(os)
        share_null_device(os, subprocess, os.environ["TMP"])
    with contextlib.suppress(OSError):  # model code may have removed it; tempfile needs it
        Path(os.environ["TMP"]).mkdir(exist_ok=True)
    # Figures become images in the run result, never windows on the machine running FL.
    os.environ["MPLBACKEND"] = "module://fl_mcp.kernel.mpl_inline"
    inbound, outbound = _take_wire()
    channel = Channel(inbound, outbound)
    # -I keeps the workspace off sys.path while the kernel itself imports;
    # model code then gets it, and the packages it installs, like a notebook.
    sys.path.extend([str(Path.cwd()), os.environ["PIP_TARGET"]])
    namespace = new_namespace(sdk.FL(RemoteSession(channel)))
    probe = boundary_probe(args.probe, args.probe_home, args.probe_port) if args.probe else None
    channel.send(wire.Ready(pid=os.getpid(), probe=probe))
    channel.serve(namespace)


if __name__ == "__main__":
    main()
