"""UdpCarrier: the UDP client half of the spool protocol against FakeBridge
(carrier_fakes). Framing, retransmit, token auth, and the is_live capability
probe; id allocation belongs to Transport (test_transport.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from carrier_fakes import TOKEN, FakeBridge

from fl_mcp import errors
from fl_mcp.core.carriers.udp import (
    PORT_FILE,
    BridgeEndpoint,
    BridgeUnavailable,
    UdpCarrier,
    discover,
)
from fl_mcp.schemas import SpoolCommand, SpoolResponse


@pytest.fixture
def fake_bridge(tmp_path: Path):
    bridge = FakeBridge(tmp_path / "root")
    yield bridge
    bridge.close()


def carrier_for(bridge: FakeBridge) -> UdpCarrier:
    carrier = UdpCarrier(bridge.roots)
    assert carrier.is_live()  # endpoint + pong: the carrier connects itself
    return carrier


def ping_cmd(cmd_id: int) -> SpoolCommand:
    return SpoolCommand(id=cmd_id, op="ping", args={})


def test_discover_returns_none_without_port_file(tmp_path: Path) -> None:
    assert discover([tmp_path]) is None


def test_discover_ignores_malformed_port_file(tmp_path: Path) -> None:
    (tmp_path / PORT_FILE).write_text(json.dumps({"port": 70000, "token": "short"}))
    assert discover([tmp_path]) is None


def test_discover_reads_endpoint(fake_bridge: FakeBridge) -> None:
    endpoint = discover(fake_bridge.roots)
    assert endpoint == BridgeEndpoint(
        proto=2, transport=1, port=fake_bridge.port, token=TOKEN, pid=1
    )


def test_round_trip_add_notes(fake_bridge: FakeBridge) -> None:
    carrier = carrier_for(fake_bridge)
    cmd = SpoolCommand(
        id=1,
        op="add_notes",
        args={
            "target": {"pattern": 1, "channel": 0},
            "notes": [{"key": 60, "start": 0.0, "length": 1.0}, {"key": 64, "start": 1.0, "length": 1.0}],
        },
    )
    carrier.send(cmd)
    response = carrier.await_response(cmd.id, timeout=2.0)
    assert response == SpoolResponse(id=cmd.id, ok=True, op="add_notes", notes_written=2)
    # The bridge saw the SpoolCommand JSON verbatim: same model, same id.
    # received[0] is is_live's id-less ping; the command follows it.
    assert fake_bridge.received[-1]["id"] == cmd.id
    assert fake_bridge.received[-1]["proto"] == cmd.proto
    assert [n["key"] for n in fake_bridge.received[-1]["args"]["notes"]] == [60, 64]


def test_ping_round_trip_is_idless(fake_bridge: FakeBridge) -> None:
    assert carrier_for(fake_bridge).ping(timeout=2.0) is True
    # Heartbeat probes carry no id, so the bridge never claims a ledger slot.
    assert all("id" not in received for received in fake_bridge.received)


def test_retransmit_recovers_a_dropped_datagram(fake_bridge: FakeBridge) -> None:
    carrier = carrier_for(fake_bridge)
    fake_bridge.drop_first = 1
    carrier.send(ping_cmd(1))
    response = carrier.await_response(1, timeout=3.0)
    assert response.ok
    with_id = [c for c in fake_bridge.received if "id" in c]
    assert len(with_id) >= 2  # the drop plus at least one retransmit
    assert {c["id"] for c in with_id} == {1}  # same command, same id


def test_timeout_raises_the_note_planes_retryable_error(fake_bridge: FakeBridge) -> None:
    carrier = carrier_for(fake_bridge)
    fake_bridge.silent = True
    carrier.send(ping_cmd(1))
    with pytest.raises(errors.ToolError) as excinfo:
        carrier.await_response(1, timeout=0.3)
    assert excinfo.value.category == "note_plane_unavailable"
    assert excinfo.value.retryable


def test_acks_with_a_wrong_token_are_ignored(fake_bridge: FakeBridge) -> None:
    carrier = carrier_for(fake_bridge)
    fake_bridge.ack_token = "f" * 32
    carrier.send(ping_cmd(1))
    with pytest.raises(errors.ToolError):
        carrier.await_response(1, timeout=0.4)


def test_oversized_command_is_refused_toward_the_file_spool(fake_bridge: FakeBridge) -> None:
    carrier = carrier_for(fake_bridge)
    notes = [{"key": 60, "start": float(i), "length": 1.0} for i in range(5000)]
    cmd = SpoolCommand(
        id=1, op="add_notes", args={"target": {"pattern": 1, "channel": 0}, "notes": notes}
    )
    with pytest.raises(BridgeUnavailable, match="file spool"):
        carrier.send(cmd)


def test_is_live_true_when_the_bridge_pongs(fake_bridge: FakeBridge) -> None:
    # THE carrier decision (R4 B3-1): a live endpoint means UDP, end to end.
    carrier = UdpCarrier(fake_bridge.roots)
    assert carrier.is_live() is True
    carrier.send(ping_cmd(1))
    assert carrier.await_response(1, timeout=2.0).ok
    carrier.close()


def test_is_live_false_without_an_endpoint(tmp_path: Path) -> None:
    assert UdpCarrier([tmp_path]).is_live() is False


def test_is_live_false_when_the_endpoint_is_stale(tmp_path: Path) -> None:
    # A port file left behind by a crashed FL validates but cannot pong;
    # freshness is the round trip, not the file (R4 B2-7).
    bridge = FakeBridge(tmp_path / "root", silent=True)
    try:
        assert UdpCarrier(bridge.roots, ping_timeout=0.3).is_live() is False
    finally:
        bridge.close()


def test_close_is_idempotent(fake_bridge: FakeBridge) -> None:
    carrier = carrier_for(fake_bridge)
    carrier.close()
    carrier.close()
