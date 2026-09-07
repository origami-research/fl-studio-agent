"""A fake native Bridge speaking the UDP ack shape, shared by the udp-carrier
and transport tests.

The fake answers exactly like native/bridge does (same frame, same result
fields - see bridge_udp_test.cpp), so these tests pin the client half of the
protocol without FL or the native plugin in the loop.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

from fl_mcp.core.carriers.udp import MAGIC, PORT_FILE

TOKEN = "deadbeefdeadbeefdeadbeefdeadbeef"


class FakeBridge:
    """A UDP responder that acks like the native bridge's executor."""

    def __init__(
        self,
        root: Path,
        *,
        token: str = TOKEN,
        drop_first: int = 0,
        ack_token: str | None = None,
        silent: bool = False,
    ) -> None:
        self.token = token
        self.ack_token = ack_token if ack_token is not None else token
        self.drop_first = drop_first
        self.silent = silent
        self.received: list[dict] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.05)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        root.mkdir(parents=True, exist_ok=True)
        (root / PORT_FILE).write_text(
            json.dumps({"proto": 2, "transport": 1, "port": self.port, "token": token, "pid": 1})
        )
        self.roots = [root]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def _serve(self) -> None:
        header = MAGIC + self.token.encode() + b" "
        while not self._stop.is_set():
            try:
                datagram, addr = self._sock.recvfrom(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if not datagram.startswith(header):
                continue  # the real bridge drops unauthenticated frames silently
            command = json.loads(datagram[len(header) :])
            self.received.append(command)
            if self.drop_first > 0:
                self.drop_first -= 1
                continue
            if self.silent:
                continue
            ack = self._ack_for(command)
            frame = MAGIC + self.ack_token.encode() + b" " + json.dumps(ack).encode()
            self._sock.sendto(frame, addr)

    def _ack_for(self, command: dict) -> dict:
        # Field-for-field what bridge_core::ExecuteCommand emits. An id-less
        # ping (the heartbeat probe) is answered statelessly with id 0.
        if command["op"] == "ping":
            return {
                "id": command.get("id", 0),
                "ok": True,
                "op": "ping",
                "proto": 2,
                "backend": "bridge",
            }
        notes = command.get("args", {}).get("notes", [])
        return {
            "id": command["id"],
            "ok": True,
            "op": command["op"],
            "notes_written": len(notes),
            "consumed": True,
            "backend": "bridge",
            "spool": "udp",
        }


class _ControlPlatform:
    """The real Platform for this OS, except the control wire is the given fake.

    create_session() selects the control carrier through Platform.control_factory(),
    so tests inject a fake here instead of patching a module attribute.
    """

    def __init__(self, control):
        import sys

        from fl_mcp.platform.runtime import platform_for

        self._base = platform_for(sys.platform)
        self._control = control

    def control_factory(self):
        return lambda: self._control

    def __getattr__(self, name):
        return getattr(self._base, name)


def control_platform(control):
    return _ControlPlatform(control)
