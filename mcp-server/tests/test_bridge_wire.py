"""bridge_wire: the UDP client against a fake bridge speaking the ack shape.

The fake answers exactly like native/bridge does (same frame, same result
fields - see bridge_udp_test.cpp), so these tests pin the client half of the
protocol without FL or the native plugin in the loop.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from fl_mcp import errors
from fl_mcp.bridge_wire import (
    MAGIC,
    PORT_FILE,
    BridgeEndpoint,
    BridgeUnavailable,
    BridgeWire,
    discover,
)
from fl_mcp.schemas import SpoolResponse

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
            json.dumps({"proto": 1, "transport": 1, "port": self.port, "token": token, "pid": 1})
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
        # Field-for-field what bridge_core::ExecuteCommand emits.
        if command["op"] == "ping":
            return {"id": command["id"], "ok": True, "op": "ping", "proto": 1, "backend": "bridge"}
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


@pytest.fixture
def fake_bridge(tmp_path: Path):
    bridge = FakeBridge(tmp_path / "root")
    yield bridge
    bridge.close()


def wire_for(bridge: FakeBridge) -> BridgeWire:
    endpoint = discover(bridge.roots)
    assert endpoint is not None
    return BridgeWire(endpoint, roots=bridge.roots)


def test_discover_returns_none_without_port_file(tmp_path: Path) -> None:
    assert discover([tmp_path]) is None


def test_discover_ignores_malformed_port_file(tmp_path: Path) -> None:
    (tmp_path / PORT_FILE).write_text(json.dumps({"port": 70000, "token": "short"}))
    assert discover([tmp_path]) is None


def test_discover_reads_endpoint(fake_bridge: FakeBridge) -> None:
    endpoint = discover(fake_bridge.roots)
    assert endpoint == BridgeEndpoint(
        proto=1, transport=1, port=fake_bridge.port, token=TOKEN, pid=1
    )


def test_send_rejects_unknown_op(fake_bridge: FakeBridge) -> None:
    wire = wire_for(fake_bridge)
    with pytest.raises(ValueError, match="unknown spool op"):
        wire.send("explode")


def test_round_trip_add_notes(fake_bridge: FakeBridge) -> None:
    wire = wire_for(fake_bridge)
    cmd = wire.send(
        "add_notes",
        target={"pattern": 1, "channel": 0},
        notes=[{"key": 60, "start": 0.0, "length": 1.0}, {"key": 64, "start": 1.0, "length": 1.0}],
    )
    response = wire.await_response(cmd, timeout=2.0)
    assert response == SpoolResponse(id=cmd.id, ok=True, op="add_notes", notes_written=2)
    # The bridge saw the SpoolCommand JSON verbatim: same model, same id.
    assert fake_bridge.received[0]["id"] == cmd.id
    assert fake_bridge.received[0]["proto"] == cmd.proto
    assert [n["key"] for n in fake_bridge.received[0]["args"]["notes"]] == [60, 64]


def test_ping_round_trip(fake_bridge: FakeBridge) -> None:
    assert wire_for(fake_bridge).ping(timeout=2.0) is True


def test_retransmit_recovers_a_dropped_datagram(tmp_path: Path) -> None:
    bridge = FakeBridge(tmp_path / "root", drop_first=1)
    try:
        wire = wire_for(bridge)
        cmd = wire.send("ping")
        response = wire.await_response(cmd, timeout=3.0)
        assert response.ok
        assert len(bridge.received) >= 2  # the drop plus at least one retransmit
        assert {c["id"] for c in bridge.received} == {cmd.id}  # same command, same id
    finally:
        bridge.close()


def test_timeout_raises_the_note_planes_retryable_error(tmp_path: Path) -> None:
    bridge = FakeBridge(tmp_path / "root", silent=True)
    try:
        wire = wire_for(bridge)
        cmd = wire.send("ping")
        with pytest.raises(errors.ToolError) as excinfo:
            wire.await_response(cmd, timeout=0.3)
        assert excinfo.value.category == "note_plane_unavailable"
        assert excinfo.value.retryable
    finally:
        bridge.close()


def test_acks_with_a_wrong_token_are_ignored(tmp_path: Path) -> None:
    bridge = FakeBridge(tmp_path / "root", ack_token="f" * 32)
    try:
        wire = wire_for(bridge)
        cmd = wire.send("ping")
        with pytest.raises(errors.ToolError):
            wire.await_response(cmd, timeout=0.4)
    finally:
        bridge.close()


def test_ping_false_when_bridge_is_silent(tmp_path: Path) -> None:
    bridge = FakeBridge(tmp_path / "root", silent=True)
    try:
        assert wire_for(bridge).ping(timeout=0.3) is False
    finally:
        bridge.close()


def test_oversized_command_is_refused_toward_the_file_spool(fake_bridge: FakeBridge) -> None:
    wire = wire_for(fake_bridge)
    notes = [{"key": 60, "start": float(i), "length": 1.0} for i in range(5000)]
    with pytest.raises(BridgeUnavailable, match="file spool"):
        wire.send("add_notes", target={"pattern": 1, "channel": 0}, notes=notes)


def test_ids_resume_past_existing_spool_state(fake_bridge: FakeBridge) -> None:
    # The ledger is shared with the file spool, so the id space must be too.
    root = fake_bridge.roots[0]
    (root / "consumed.json").write_text(json.dumps({"ids": [7000]}))
    wire = wire_for(fake_bridge)
    assert wire.send("ping").id == 7001
