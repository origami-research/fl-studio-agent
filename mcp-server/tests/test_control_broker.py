"""The control broker + BrokerControl client, end to end over a real unix
socket with only the MIDI wire faked.

This exercises everything the live path does except the hardware round trip:
pid addressing, socket framing, response correlation, error passthrough, and
concurrent clients sharing one wire.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

import pytest

from fl_mcp.core.carriers.broker_client import BrokerControl
from fl_mcp.core.carriers.control_broker import ControlBroker
from fl_mcp.errors import ToolError
from fl_mcp.schemas import ControlResponse


class FakeWire:
    """Records every request and echoes the pid it was addressed with, so a
    test can prove the tag survived the socket hop to the wire."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._id = 0

    def send_request(self, op, args=None, kwargs=None, timeout=2.0, pid=None):
        self.calls.append({"op": op, "args": args, "kwargs": kwargs, "timeout": timeout, "pid": pid})
        self._id += 1
        if op == "boom":
            raise RuntimeError("wire exploded")
        if op == "busy":
            from fl_mcp.errors import control_plane_busy
            raise control_plane_busy(op, timeout)
        ret = {"script_ver": "13", "fl_ver": "26.1.5", "ppq": 96} if op == "ctl.hello" else {"pid": pid, "op": op}
        return ControlResponse(id=self._id, ok=True, ret=ret)


@pytest.fixture
def broker():
    # AF_UNIX paths are capped near 104 chars on macOS; pytest tmp_path blows
    # past that, so the socket lives in a short scratch dir.
    wire = FakeWire()
    sock = Path(tempfile.mkdtemp(dir="/tmp")) / "b.sock"
    b = ControlBroker(sock, wire_factory=lambda: wire)
    assert b.bind() is True
    thread = threading.Thread(target=b.serve_forever, daemon=True)
    thread.start()
    return sock, wire


def session_with_pid(tmp_path, pid: int):
    root = tmp_path / f"session-{pid}"
    (root).mkdir()
    (root / "bridge_udp.json").write_text(json.dumps({"proto": 2, "port": 1, "token": "t", "pid": pid}))
    return root


def test_request_carries_the_session_pid_all_the_way_to_the_wire(broker, tmp_path):
    sock, wire = broker
    control = BrokerControl(session_with_pid(tmp_path, 4242), socket_path=sock)

    resp = control.send_request("mixer.setCurrentTempo", args=[140000], timeout=3.0)

    assert resp.ok is True
    assert wire.calls[0]["op"] == "mixer.setCurrentTempo"
    assert wire.calls[0]["pid"] == 4242, "the wire must receive this session's pid"
    assert wire.calls[0]["args"] == [140000]


def test_hello_round_trips_and_marks_live(broker, tmp_path):
    sock, _ = broker
    control = BrokerControl(session_with_pid(tmp_path, 7), socket_path=sock)
    assert control.is_live() is False
    hello = control.await_hello(timeout=3.0)
    assert hello.ppq == 96
    assert control.is_live() is True


def test_two_sessions_share_one_broker_without_crosstalk(broker, tmp_path):
    sock, wire = broker
    a = BrokerControl(session_with_pid(tmp_path, 100), socket_path=sock)
    b = BrokerControl(session_with_pid(tmp_path, 200), socket_path=sock)

    a.send_request("transport.start")
    b.send_request("transport.stop")

    by_pid = {c["pid"]: c["op"] for c in wire.calls}
    assert by_pid == {100: "transport.start", 200: "transport.stop"}


def test_wire_transport_error_re_raises_as_a_typed_error_not_a_dropped_connection(broker, tmp_path):
    # A raised wire/transport error is relayed as a typed ToolError (mirroring
    # MidiControl), so the client sees the same failure it would on the raw wire
    # - and the broker connection is not dropped for other sessions.
    sock, _ = broker
    control = BrokerControl(session_with_pid(tmp_path, 9), socket_path=sock)
    with pytest.raises(ToolError) as exc:
        control.send_request("boom", timeout=3.0)
    assert "wire exploded" in exc.value.message


def test_missing_bridge_port_file_is_an_actionable_error(broker, tmp_path):
    sock, _ = broker
    empty = tmp_path / "no-bridge"
    empty.mkdir()
    control = BrokerControl(empty, socket_path=sock)
    with pytest.raises(ToolError) as exc:
        control.send_request("ping", timeout=1.0)
    assert "bridge port file" in exc.value.message


def test_second_broker_bind_yields_to_the_first(broker, tmp_path):
    sock, _ = broker
    second = ControlBroker(sock, wire_factory=FakeWire)
    assert second.bind() is False, "a live broker already owns the socket"


def test_busy_wire_relays_as_busy_not_dead(broker, tmp_path):
    # The category must survive the broker hop so liveness checks treat a
    # contended wire as live (a retry), not a dead FL (a recycle).
    sock, _ = broker
    control = BrokerControl(session_with_pid(tmp_path, 5), socket_path=sock)
    with pytest.raises(ToolError) as exc:
        control.send_request("busy", timeout=1.0)
    assert exc.value.category == "control_plane_busy"
