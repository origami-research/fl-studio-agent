"""CtlSpoolControl: per-session control over the pid-addressed ctl file lane.

The responder in these tests plays the controller script's role byte-for-byte
(read ctl_cmd_<id>.json, write ctl_resp_<id>.json) so the protocol the server
speaks is the one device_FLMCP.py actually implements.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from fl_mcp.core.carriers import ctlspool
from fl_mcp.core.carriers.ctlspool import CtlSpoolControl
from fl_mcp.errors import ToolError


@pytest.fixture
def lanes(tmp_path, monkeypatch):
    """A session spool (with a bridge port file) and a shared ctl root."""
    session_root = tmp_path / "session-spool"
    session_root.mkdir()
    (session_root / "bridge_udp.json").write_text(
        json.dumps({"proto": 2, "transport": 1, "port": 1, "token": "t", "pid": 4242})
    )
    ctl_root = tmp_path / "settings" / "Piano roll scripts" / "FLMCP"
    ctl_root.mkdir(parents=True)
    monkeypatch.setattr(ctlspool, "fl_settings_dirs", lambda: [tmp_path / "settings"])
    return session_root, ctl_root


def respond(ctl_root: Path, handler) -> threading.Thread:
    """Play the controller: answer the first command that appears."""

    def run() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            commands = sorted(ctl_root.glob("ctl_cmd_*.json"))
            for path in commands:
                try:
                    command = json.loads(path.read_text())
                except ValueError:
                    continue
                (ctl_root / f"ctl_resp_{command['id']}.json").write_text(
                    json.dumps(handler(command))
                )
                return
            time.sleep(0.01)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_round_trip_carries_the_session_pid_and_cleans_its_files(lanes):
    session_root, ctl_root = lanes
    seen: dict = {}

    def handler(command):
        seen.update(command)
        return {"id": command["id"], "ok": True, "ret": "pong"}

    thread = respond(ctl_root, handler)
    control = CtlSpoolControl(session_root)
    response = control.send_request("ping", timeout=5.0)
    thread.join()

    assert response.ok is True and response.ret == "pong"
    assert seen["op"] == "ping"
    assert seen["pid"] == 4242, "the command must be addressed to the session's FL pid"
    assert seen["id"] > (1 << ctlspool._ID_PID_SHIFT), "ids are namespaced by server pid"
    assert list(ctl_root.iterdir()) == [], "the server owns and removes both files"


def test_hello_is_a_ctl_round_trip_and_marks_the_lane_live(lanes):
    session_root, ctl_root = lanes
    respond(ctl_root, lambda command: {
        "id": command["id"], "ok": True,
        "ret": {"script_ver": "13", "fl_ver": "26.1.5", "ppq": 96, "tempo": 120.0},
    })
    control = CtlSpoolControl(session_root)

    assert control.is_live() is False
    hello = control.await_hello(timeout=5.0)
    assert hello.ppq == 96
    assert control.is_live() is True


def test_missing_bridge_port_file_is_an_actionable_error(tmp_path, monkeypatch):
    session_root = tmp_path / "empty-session"
    session_root.mkdir()
    monkeypatch.setattr(ctlspool, "fl_settings_dirs", lambda: [tmp_path])
    control = CtlSpoolControl(session_root)

    with pytest.raises(ToolError) as exc_info:
        control.send_request("ping", timeout=0.1)
    assert "bridge port file" in exc_info.value.message
    assert exc_info.value.retryable is True


def test_timeout_distinguishes_absent_from_busy(lanes):
    session_root, ctl_root = lanes
    control = CtlSpoolControl(session_root)

    with pytest.raises(ToolError) as absent:
        control.send_request("ping", timeout=0.05)
    assert "busy" not in absent.value.message

    respond(ctl_root, lambda command: {
        "id": command["id"], "ok": True,
        "ret": {"script_ver": "13", "fl_ver": "26.1.5", "ppq": 96},
    })
    control.await_hello(timeout=5.0)
    with pytest.raises(ToolError) as busy:
        control.send_request("ping", timeout=0.05)
    assert "busy" in busy.value.message


def test_session_mode_selects_the_ctl_lane_and_never_opens_the_wire(tmp_path, monkeypatch):
    # In session mode the MIDI wire must not even be constructed: its fixed
    # port names cross-connect concurrent sessions (#150).
    from fl_mcp.session import facade

    monkeypatch.setenv("FLMCP_SPOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(
        facade, "MidiControl",
        lambda: (_ for _ in ()).throw(AssertionError("the wire was opened in session mode")),
    )
    session = facade.create_session()
    try:
        assert isinstance(session.control, CtlSpoolControl)
        assert session.control.target_pid() is None  # no bridge yet - honest, not fatal
    finally:
        if session.control is not None:
            session.control.close()
