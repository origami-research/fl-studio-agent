"""A session's control carrier: talk to the machine's one control broker.

This is what fl-mcp uses for control in session mode. It implements the same
ControlCarrier surface as the raw wire, so nothing upstream (the SDK, the MCP
tools, the arena) changes - the wire simply lives in a shared broker instead
of this process, and every request is addressed to this session's FL by the
pid in its bridge port file.

The broker is auto-started on first use: if the socket does not answer, spawn
``fl-mcp-control-broker`` and wait for it. Startup is race-free by the broker's
socket bind (first wins, others exit), so concurrent sessions cannot create two
wires - the failure mode that measured 0/10.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from fl_mcp.contract import BRIDGE_PORT_FILE
from fl_mcp.core.carriers.control_broker import default_socket_path
from fl_mcp.errors import ToolError, control_plane_unavailable
from fl_mcp.schemas import ControlResponse, HelloEvent

_BROKER_START_TIMEOUT = 10.0


class BrokerControl:
    """ControlCarrier over the shared broker, pid-addressed per session."""

    def __init__(self, session_spool_root: Path, socket_path: Path | None = None) -> None:
        self._session_spool_root = session_spool_root
        self._socket_path = socket_path or default_socket_path()
        self._hello: HelloEvent | None = None
        self._broker_spawned = False

    # -- identity ---------------------------------------------------------------

    def target_pid(self) -> int | None:
        """This session's FL pid from its bridge port file - re-read per call
        because FL re-execs at boot and can relaunch mid-session."""
        try:
            payload = json.loads((self._session_spool_root / BRIDGE_PORT_FILE).read_text())
        except (OSError, ValueError):
            return None
        pid = payload.get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None

    # -- the carrier contract ----------------------------------------------------

    def is_live(self) -> bool:
        return self._hello is not None

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        if self._hello is not None:
            return self._hello
        response = self.send_request("ctl.hello", timeout=timeout)
        if not response.ok or not isinstance(response.ret, dict):
            raise control_plane_unavailable("ctl.hello", timeout)
        self._hello = HelloEvent.model_validate(response.ret)
        return self._hello

    def send_request(
        self,
        op: str,
        args: list | None = None,
        kwargs: dict | None = None,
        timeout: float = 2.0,
    ) -> ControlResponse:
        pid = self.target_pid()
        if pid is None:
            raise ToolError(
                "control_plane_unavailable",
                f"No response to {op!r}: this session's FL has not published its "
                f"bridge port file under {self._session_spool_root}.",
                "Wait for the session's FL to finish loading its project (the "
                "bridge publishes bridge_udp.json), then retry.",
                retryable=True,
            )
        request = {"pid": pid, "op": op, "args": args or [], "kwargs": kwargs or {}, "timeout": timeout}
        # The socket round trip may take as long as the wire op plus broker
        # hop; give it headroom over the wire timeout.
        conn = self._connect()
        try:
            conn.sendall((json.dumps(request) + "\n").encode())
            conn.settimeout(timeout + 5.0)
            payload = conn.makefile("r").readline()
        finally:
            conn.close()
        if not payload:
            raise control_plane_unavailable(op, timeout)
        envelope = json.loads(payload)
        # Mirror the wire's contract: a transport failure the broker relays is
        # re-raised as the SAME typed error (so a busy wire stays 'busy', not
        # mistaken for a dead one); an FL-level answer is returned.
        if envelope.get("kind") == "error":
            raise ToolError(envelope["category"], envelope["message"], envelope.get("fix", ""), retryable=envelope.get("retryable", True))
        return ControlResponse.model_validate(envelope["response"])

    def _connect(self) -> socket.socket:
        """Connect to the broker, auto-starting it once if absent."""
        if (conn := self._try_connect()) is not None:
            return conn
        if not self._broker_spawned:
            # The bind race is resolved broker-side (first wins), so another
            # client may launch the process that eventually answers us.
            self._broker_spawned = True
            subprocess.Popen(
                [sys.executable, "-m", "fl_mcp.control_broker"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        deadline = time.monotonic() + _BROKER_START_TIMEOUT
        while time.monotonic() < deadline:
            if (conn := self._try_connect()) is not None:
                return conn
            time.sleep(0.1)
        raise ToolError(
            "control_plane_unavailable",
            f"The control broker did not come up at {self._socket_path} within "
            f"{_BROKER_START_TIMEOUT:g}s.",
            "Check that fl-mcp-control-broker can start (python-rtmidi installed).",
            retryable=True,
        )

    def _try_connect(self) -> socket.socket | None:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(str(self._socket_path))
        except OSError:
            conn.close()
            return None
        return conn

    # -- push surface: the broker relays round trips only ------------------------

    def add_event_listener(self, fn) -> None:
        """Push events are not relayed through the broker (bridge covers them)."""

    def drain_events(self) -> list:
        return []

    @property
    def dropped_frames(self) -> int:
        return 0

    def close(self) -> None:
        """Per-request connections are already closed; the broker persists."""
