"""Session audio lifecycle: the relay exists without a client request."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from carrier_fakes import FakeBridge
from session_fakes import make_session

from fl_mcp.core.carriers.spool import SpoolCarrier
from fl_mcp.core.carriers.udp import UdpCarrier
from fl_mcp.core.transport import Transport
from fl_mcp.schemas import PlaneStatus, SpoolCommand, SpoolResponse
from fl_mcp.session import detect as detect_mod
from fl_mcp.session import state as state_mod
from fl_mcp.session.detect import read_heartbeat

# The session's wall clock, pinned to the heartbeat's own stamp. Heartbeat
# freshness is wall-clock age (the Bridge stamps unix_time), so a host that
# sleeps or steps its clock between writing this one-shot heartbeat and the
# lifecycle reading it saw a "stale Bridge" and never started the stream.
NOW = 1_800_000_000


def _clock() -> float:
    return NOW


def _write_heartbeat(root: Path, *, chunks: int = 0, drops: int = 0) -> None:
    root.joinpath("bridge_heartbeat.txt").write_text(
        "unix_time=%d\nproto=2\nabi=ok\nrender_rate=1\nstreaming=1\n"
        "stream_chunks_sent=%d\nstream_chunks_dropped=%d\nstream_sample_rate=48000\n"
        % (NOW, chunks, drops),
        encoding="utf-8",
    )


def _session(root: Path):
    transport = Transport([UdpCarrier([root]), SpoolCarrier(root)], roots=[root])
    session, _ = make_session(transport=transport, session_spool_root=root, clock=_clock)
    return session


# Waits on the lifecycle are bounded in its own passes, not in seconds (#258).
# One pass can legitimately take about 3 s on a loaded runner: a 0.5 s UDP ping
# that misses, a 2 s stream_start timeout on the file carrier, then the retry
# on the next pass. A fixed 3 s deadline therefore measured the runner, not the
# lifecycle. Counting passes ties the bound to the lifecycle's own retries
# ("publishes within a few polls"), however slow the machine is.
PASS_BUDGET = 10
# Only reports a dead or wedged thread; a passing run never waits this long.
HANG_GUARD_SECONDS = 30.0


class _LifecyclePasses:
    """Counts the session audio lifecycle's completed passes."""

    def __init__(self, monkeypatch, session) -> None:
        self._done = threading.Condition()
        self._count = 0
        run_pass = session._audio_stream_pass

        def counted() -> None:
            try:
                run_pass()
            finally:
                with self._done:
                    self._count += 1
                    self._done.notify_all()

        monkeypatch.setattr(session, "_audio_stream_pass", counted)

    def wait_until(self, condition: Callable[[], bool], what: str) -> None:
        with self._done:
            start = self._count
            while not condition():
                if self._count - start >= PASS_BUDGET:
                    raise AssertionError(f"{what} within {PASS_BUDGET} lifecycle passes")
                if not self._done.wait(HANG_GUARD_SECONDS):
                    raise AssertionError(f"the audio lifecycle stopped polling: {what}")


def test_session_lifecycle_publishes_and_removes_audio_endpoint(monkeypatch, tmp_path) -> None:
    root = tmp_path / "spool"
    bridge = FakeBridge(root)
    _write_heartbeat(root)
    session = _session(root)
    monkeypatch.setenv("FLMCP_AUDIO_STREAM", "1")
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [root])
    passes = _LifecyclePasses(monkeypatch, session)
    try:
        session.start_audio_stream_lifecycle()
        port_file = root / "bridge_audio.json"
        passes.wait_until(port_file.exists, "bridge_audio.json was not published")
        payload = json.loads(port_file.read_text())
        assert payload == {"host": "127.0.0.1", "port": payload["port"], "proto": 1, "sample_rate": 44_100}
        assert any(command["op"] == "stream_start" for command in bridge.received)
    finally:
        session.close()
        bridge.close()
    assert not (root / "bridge_audio.json").exists()


def test_live_session_prewarm_and_audio_lifecycle_both_complete(monkeypatch, tmp_path) -> None:
    """A session with the optional relay still completes its startup ping.

    FakeBridge is the offline native-Bridge seam: it answers the same UDP
    command frames as FL.  This covers the arena shape without launching FL.
    """
    root = tmp_path / "spool"
    bridge = FakeBridge(root)
    _write_heartbeat(root)
    session = _session(root)
    monkeypatch.setenv("FLMCP_AUDIO_STREAM", "1")
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [root])
    passes = _LifecyclePasses(monkeypatch, session)
    try:
        session.prewarm()
        session.start_audio_stream_lifecycle()
        passes.wait_until((root / "bridge_audio.json").exists, "bridge_audio.json was not published")
        assert [command["op"] for command in bridge.received if "id" in command] == ["ping", "stream_start"]
    finally:
        session.close()
        bridge.close()


class _BlockingStreamTransport:
    """Makes an in-flight stream request observable without a real socket."""

    def __init__(self) -> None:
        self.stream_sent = threading.Event()
        self.release_stream = threading.Event()
        self.ping_sent = threading.Event()
        self._next_id = 0

    def send(self, op: str, **args) -> SpoolCommand:
        self._next_id += 1
        if op == "stream_start":
            self.stream_sent.set()
        if op == "ping":
            self.ping_sent.set()
        return SpoolCommand(id=self._next_id, op=op, args=args)

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        if command.op == "stream_start":
            assert self.release_stream.wait(timeout)
        return SpoolResponse(id=command.id, ok=True, op=command.op)


def test_audio_lifecycle_does_not_interleave_a_session_transport_request(monkeypatch, tmp_path) -> None:
    """The lifecycle cannot consume the response belonging to startup ping.

    This failed before the session transport transaction existed: prewarm
    sent ping while stream_start was awaiting its reply on the same transport.
    """
    root = tmp_path / "spool"
    root.mkdir()
    root.joinpath("bridge_udp.json").write_text('{"pid": 1}', encoding="utf-8")
    _write_heartbeat(root)
    transport = _BlockingStreamTransport()
    session, _ = make_session(transport=transport, session_spool_root=root, clock=_clock)
    monkeypatch.setenv("FLMCP_AUDIO_STREAM", "1")
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [root])
    try:
        session.start_audio_stream_lifecycle()
        assert transport.stream_sent.wait(HANG_GUARD_SECONDS)
        prewarm = threading.Thread(target=session.prewarm)
        prewarm.start()
        time.sleep(0.05)
        assert not transport.ping_sent.is_set()
        transport.release_stream.set()
        prewarm.join(timeout=HANG_GUARD_SECONDS)
        assert not prewarm.is_alive()
        assert transport.ping_sent.is_set()
    finally:
        transport.release_stream.set()
        session.close()


def test_bridge_pid_change_restarts_and_republishes_audio_endpoint(monkeypatch, tmp_path) -> None:
    root = tmp_path / "spool"
    first = FakeBridge(root, pid=1)
    _write_heartbeat(root)
    session = _session(root)
    monkeypatch.setenv("FLMCP_AUDIO_STREAM", "1")
    passes = _LifecyclePasses(monkeypatch, session)
    try:
        session.start_audio_stream_lifecycle()
        port_file = root / "bridge_audio.json"
        passes.wait_until(port_file.exists, "bridge_audio.json was not published")
        first.close()
        second = FakeBridge(root, pid=2)
        _write_heartbeat(root)

        # The pass that sees pid 2 unpublishes the old endpoint before it sends
        # stream_start, so a port file seen after the new Bridge has that request
        # is the new publish (check in that order). Comparing ports was not
        # proof: the OS may hand the freed port straight to the new listener.
        def republished() -> bool:
            return any(command["op"] == "stream_start" for command in second.received) and port_file.exists()

        passes.wait_until(republished, "audio endpoint was not republished after Bridge restart")
        port = json.loads(port_file.read_text())["port"]
        socket.create_connection(("127.0.0.1", port), timeout=HANG_GUARD_SECONDS).close()
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

    status = state_mod._note_plane_status(bridge_installed=True, clock=_clock)

    assert status == PlaneStatus(
        connected=True, detail=f"spool at {root}; live audio streaming; chunks=8, drops=2, sample_rate=48000"
    )
    heartbeat = read_heartbeat(root / "bridge_heartbeat.txt", clock=_clock)
    assert heartbeat.stream_chunks_dropped == 2
