"""Transport: the one send path over carriers (docs/30 Surface 2).

Validated + normalized commands, race-free ids resumed past the consumer's
ledger, the per-call live-carrier pick (UDP when it pongs, the file spool
otherwise), and the #58 exactly-once invariant: a command that hops carriers
keeps its id, a command that times out never hops.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from carrier_fakes import FakeBridge
from pydantic import ValidationError

from fl_mcp.core.carriers import CarrierUnavailable
from fl_mcp.core.carriers.spool import SpoolCarrier, read_json, write_json_atomic
from fl_mcp.core.carriers.udp import UdpCarrier
from fl_mcp.core.transport import Transport
from fl_mcp.errors import ToolError

NOTE = {"key": "C4", "start": 0, "length": 1}
TARGET = {"pattern": 1, "channel": 0}


def spool_transport(tmp_path) -> tuple[Transport, Path]:
    root = tmp_path / "spoolroot"
    return Transport([SpoolCarrier(root)], roots=[root]), root


def dual_transport(bridge: FakeBridge, *, ping_timeout: float = 0.5) -> tuple[Transport, UdpCarrier, Path]:
    """The post-rewire wiring: UDP first, file spool fallback, one id space."""
    root = bridge.roots[0]
    udp = UdpCarrier(bridge.roots, ping_timeout=ping_timeout)
    return Transport([udp, SpoolCarrier(root)], roots=[root]), udp, root


def respond(root: Path, cmd_id: int, **fields) -> None:
    write_json_atomic(root / "responses" / f"{cmd_id:06d}.json", {"id": cmd_id, "ok": True, **fields})


def spooled_command(root: Path, cmd_id: int) -> dict | None:
    return read_json(root / "commands" / f"{cmd_id:06d}.json")


@pytest.fixture
def fake_bridge(tmp_path: Path):
    bridge = FakeBridge(tmp_path / "root")
    yield bridge
    bridge.close()


# -- command: validation + the one id space -------------------------------------


def test_command_is_validated_normalized_and_sequenced(tmp_path):
    t, _root = spool_transport(tmp_path)
    cmd = t.command("add_notes", target=TARGET, notes=[NOTE], mode="merge")
    assert cmd.id == 1
    # Unions resolve at the boundary: the consumer must never see "C4".
    assert cmd.args["notes"][0]["key"] == 60
    assert cmd.args["notes"][0]["velocity"] == 0.8


def test_command_rejects_bad_args_and_unknown_ops(tmp_path):
    t, root = spool_transport(tmp_path)
    with pytest.raises(ValidationError):
        t.command("add_notes", target=TARGET, notes=[])  # min_length=1
    with pytest.raises(ValueError, match="unknown spool op"):
        t.command("evil_op")
    # Nothing dispatched: a rejected command reaches no carrier.
    assert not list((root / "commands").glob("0*.json"))


def test_ids_are_sequential_and_race_free(tmp_path):
    t, root = spool_transport(tmp_path)
    ids: list[int] = []

    def send_one() -> None:
        ids.append(t.send("ping").id)

    threads = [threading.Thread(target=send_one) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(ids) == list(range(1, 17)), "concurrent sends must never share an id"
    queue = read_json(root / "commands" / "queue.json")
    assert queue["ids"] == list(range(1, 17))


def test_restart_never_reuses_ids(tmp_path):
    t, root = spool_transport(tmp_path)
    t.send("ping")
    t.send("ping")

    assert Transport([SpoolCarrier(root)], roots=[root]).send("ping").id == 3

    # The ledger alone must also advance the counter: the consumer refuses ids
    # it has already applied, even when the server's own files are gone.
    write_json_atomic(root / "consumed.json", {"ids": [41]})
    assert Transport([SpoolCarrier(root)], roots=[root]).send("ping").id == 42


# -- request: the per-call carrier pick -----------------------------------------


def test_request_round_trips_on_the_file_carrier(tmp_path):
    t, root = spool_transport(tmp_path)
    cmd = t.command("ping")
    timer = threading.Timer(0.15, respond, args=(root, cmd.id), kwargs={"op": "ping"})
    timer.start()
    try:
        assert t.request(cmd, timeout=2.0).ok is True
    finally:
        timer.cancel()


def test_picks_udp_when_the_bridge_is_live(fake_bridge):
    t, _udp, root = dual_transport(fake_bridge)
    cmd = t.command("add_notes", target=TARGET, notes=[NOTE])
    resp = t.request(cmd, timeout=2.0)
    assert resp.ok and resp.notes_written == 1
    # The command travelled the datagram, not the mailbox.
    assert fake_bridge.received[-1]["id"] == cmd.id
    assert spooled_command(root, cmd.id) is None


def test_falls_back_to_spool_when_the_bridge_is_not_live(tmp_path):
    root = tmp_path / "root"
    t = Transport([UdpCarrier([root]), SpoolCarrier(root)], roots=[root])  # no port file
    cmd = t.send("ping")
    assert spooled_command(root, cmd.id)["op"] == "ping"
    respond(root, cmd.id, op="ping")
    assert t.await_response(cmd, timeout=1.0).ok is True


def test_repicks_per_call_when_the_bridge_dies(fake_bridge):
    t, udp, root = dual_transport(fake_bridge, ping_timeout=0.2)
    assert t.request(t.command("ping"), timeout=2.0).ok  # UDP, end to end
    fake_bridge.close()
    udp.close()  # the next call re-probes: stale port file, no pong -> spool
    cmd = t.send("ping")
    assert spooled_command(root, cmd.id)["op"] == "ping"


def test_no_live_carrier_raises_carrier_unavailable(tmp_path):
    t = Transport([UdpCarrier([tmp_path])], roots=[tmp_path])
    with pytest.raises(CarrierUnavailable, match="no live carrier"):
        t.request(t.command("ping"), timeout=1.0)


# -- the #58 invariant: exactly-once across carriers ----------------------------


def test_oversized_command_hops_to_spool_with_the_same_id(fake_bridge):
    # UDP refuses above the datagram ceiling at SEND time, so the command may
    # hop - and it must keep its id: the consumer's ledger is keyed by id, and
    # a fresh id here could apply the same write twice.
    t, _udp, root = dual_transport(fake_bridge)
    notes = [{"key": 60, "start": float(i), "length": 1.0} for i in range(5000)]
    cmd = t.send("add_notes", target=TARGET, notes=notes)

    assert spooled_command(root, cmd.id)["id"] == cmd.id
    assert all("id" not in received for received in fake_bridge.received)  # only is_live's ping
    respond(root, cmd.id, op="add_notes", notes_written=5000)
    assert t.await_response(cmd, timeout=1.0).notes_written == 5000

    # Whatever medium delivered, the one id space resumes past it.
    assert Transport([SpoolCarrier(root)], roots=[root]).send("ping").id == cmd.id + 1


def test_a_timeout_never_hops_carriers(fake_bridge):
    # An unanswered datagram may still apply on a later tick (docs/22);
    # re-sending it through the mailbox could double-apply. The timeout
    # surfaces to the caller's breaker instead.
    t, _udp, root = dual_transport(fake_bridge)
    assert t.request(t.command("ping"), timeout=2.0).ok
    fake_bridge.silent = True
    cmd = t.command("ping")
    with pytest.raises(ToolError) as excinfo:
        t.request(cmd, timeout=0.4)
    assert excinfo.value.category == "note_plane_unavailable"
    assert spooled_command(root, cmd.id) is None
