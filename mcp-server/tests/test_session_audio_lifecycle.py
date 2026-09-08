"""Session audio lifecycle: the relay exists without a client request."""

from __future__ import annotations

import json
import time
from pathlib import Path

from carrier_fakes import FakeBridge
from session_fakes import make_session

from fl_mcp.core.carriers.spool import SpoolCarrier
from fl_mcp.core.carriers.udp import UdpCarrier
from fl_mcp.core.transport import Transport
from fl_mcp.schemas import PlaneStatus
from fl_mcp.session import state as state_mod
from fl_mcp.session.detect import read_heartbeat


def _write_heartbeat(root: Path, *, chunks: int = 0, drops: int = 0) -> None:
    root.joinpath("bridge_heartbeat.txt").write_text(
        "unix_time=%d\nproto=2\nabi=ok\nrender_rate=1\nstreaming=1\n"
        "stream_chunks_sent=%d\nstream_chunks_dropped=%d\nstream_sample_rate=48000\n"
        % (time.time(), chunks, drops),
        encoding="utf-8",
    )


def _session(root: Path):
    transport = Transport([UdpCarrier([root]), SpoolCarrier(root)], roots=[root])
    session, _ = make_session(transport=transport, session_spool_root=root)
    return session


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was not published")


def test_session_lifecycle_publishes_and_removes_audio_endpoint(monkeypatch, tmp_path) -> None:
    root = tmp_path / "spool"
    bridge = FakeBridge(root)
    _write_heartbeat(root)
    session = _session(root)
    monkeypatch.setenv("FLMCP_AUDIO_STREAM", "1")
    try:
        session.start_audio_stream_lifecycle()
        port_file = root / "bridge_audio.json"
        _wait_for(port_file)
        payload = json.loads(port_file.read_text())
        assert payload == {"host": "127.0.0.1", "port": payload["port"], "proto": 1, "sample_rate": 44_100}
        assert any(command["op"] == "stream_start" for command in bridge.received)
    finally:
        session.close()
        bridge.close()
    assert not (root / "bridge_audio.json").exists()


def test_bridge_pid_change_restarts_and_republishes_audio_endpoint(monkeypatch, tmp_path) -> None:
    root = tmp_path / "spool"
    first = FakeBridge(root, pid=1)
    _write_heartbeat(root)
    session = _session(root)
    monkeypatch.setenv("FLMCP_AUDIO_STREAM", "1")
    try:
        session.start_audio_stream_lifecycle()
        port_file = root / "bridge_audio.json"
        _wait_for(port_file)
        first_port = json.loads(port_file.read_text())["port"]
        first.close()
        second = FakeBridge(root, pid=2)
        _write_heartbeat(root)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if port_file.exists() and json.loads(port_file.read_text())["port"] != first_port:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("audio endpoint was not republished after Bridge restart")
        assert any(command["op"] == "stream_start" for command in second.received)
    finally:
        session.close()
        second.close()


def test_doctor_reports_bridge_stream_drops(monkeypatch, tmp_path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    root.joinpath("bridge_udp.json").write_text('{"pid": 1}', encoding="utf-8")
    _write_heartbeat(root, chunks=8, drops=2)
    script = tmp_path / "MCP Write Notes.pyscript"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(state_mod, "spool_roots", lambda: [root])
    monkeypatch.setattr(state_mod, "piano_roll_scripts_dir", lambda: tmp_path)
    monkeypatch.setattr(state_mod, "pid_alive", lambda pid: pid == 1)

    status = state_mod._note_plane_status()

    assert status == PlaneStatus(
        connected=True, detail=f"spool at {root}; live audio streaming; chunks=8, drops=2, sample_rate=48000"
    )
    heartbeat = read_heartbeat(root / "bridge_heartbeat.txt")
    assert heartbeat.stream_chunks_dropped == 2
