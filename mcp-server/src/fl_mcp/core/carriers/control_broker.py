"""The one control wire for the machine (#150).

Two FL instances cannot each own a wire: virtual MIDI port names are fixed
constants, so two servers opening them collide (measured: 0/10 round trips).
But ONE wire broadcasts to every FL, and each controller runs only the
request whose pid matches its own os.getpid() (measured: 10/10, zero
crosstalk). So exactly one process owns the wire and every session addresses
its FL through it by pid.

This broker is that process. It owns a single MidiControl and serves a
newline-delimited-JSON unix socket: a client sends
``{"pid", "op", "args", "kwargs", "timeout"}`` and gets back one
``ControlResponse``-shaped line. Concurrency is safe because MidiControl
serializes round trips internally (its call lock), so the accept loop can
thread per client freely.

Ownership is decided by the socket bind, not orchestration: the first process
to bind wins and serves; a second exits. That makes auto-start race-free -
any number of sessions can try to start a broker; the machine ends with one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

from fl_mcp.core.carriers.wire import ControlUnavailable, MidiControl
from fl_mcp.errors import ToolError
from fl_mcp.fl.paths import app_data_root

log = logging.getLogger(__name__)

SOCKET_NAME = "control-broker.sock"


def default_socket_path() -> Path:
    return app_data_root() / SOCKET_NAME


class ControlBroker:
    """Serves one shared MidiControl over a unix socket, pid-addressed."""

    def __init__(self, socket_path: Path, wire_factory: Callable[[], Any] = MidiControl) -> None:
        self._socket_path = socket_path
        self._wire = wire_factory()
        self._server: socket.socket | None = None

    def bind(self) -> bool:
        """Claim the socket. False (not an error) if another broker already
        owns it - the caller should just connect to that one instead."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self._socket_path))
        except OSError:
            # Stale socket (previous broker died) vs a live one: a live broker
            # answers a connect, a stale file does not.
            if _socket_answers(self._socket_path):
                server.close()
                return False
            with contextlib.suppress(FileNotFoundError):
                self._socket_path.unlink()
            try:
                server.bind(str(self._socket_path))
            except OSError:
                server.close()
                return False
        server.listen(16)
        self._server = server
        return True

    def serve_forever(self) -> None:
        assert self._server is not None, "call bind() first"
        log.info("control broker serving at %s", self._socket_path)
        while True:
            conn, _ = self._server.accept()
            threading.Thread(target=self._serve_client, args=(conn,), daemon=True).start()

    def _serve_client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r") as reader:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                conn.sendall((json.dumps(self._handle(line)) + "\n").encode())

    def _handle(self, line: str) -> dict[str, Any]:
        """Relay one round trip, preserving the wire's raise-vs-return contract:
        a returned ControlResponse (FL answered, maybe ok=false) is wrapped
        ``kind=response``; a raised ToolError (transport: no wire, busy, timeout)
        is wrapped ``kind=error`` WITH its category, so the client re-raises the
        same typed failure - a busy wire must not look like a dead one."""
        try:
            req = json.loads(line)
            response = self._wire.send_request(
                req["op"],
                args=req.get("args") or [],
                kwargs=req.get("kwargs") or {},
                timeout=float(req.get("timeout", 2.0)),
                pid=req.get("pid"),
            )
            return {"kind": "response", "response": response.model_dump()}
        except ToolError as exc:
            return {"kind": "error", "category": exc.category, "message": exc.message, "fix": exc.fix, "retryable": exc.retryable}
        except Exception as exc:  # noqa: BLE001 - the broker boundary: a bad request must not drop the wire for other sessions
            return {"kind": "error", "category": "fl_api_error", "message": f"{type(exc).__name__}: {exc}", "fix": "Check the broker log.", "retryable": True}


def _socket_answers(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()



def main() -> None:
    """Entry point (fl-mcp-control-broker): own the wire until killed."""
    logging.basicConfig(stream=__import__("sys").stderr, level=os.environ.get("FL_MCP_LOG", "INFO").upper())
    path = default_socket_path()
    try:
        broker = ControlBroker(path)
    except ControlUnavailable:
        log.exception("control wire unavailable; broker cannot start")
        raise SystemExit(1)
    if not broker.bind():
        log.info("another control broker already owns %s; exiting", path)
        return
    broker.serve_forever()
