"""Unit tests for control_midi.MidiControl's request/response correlation,
hello tracking, and event dispatch — the parts docs/14 verified only by hand
against a live FL Studio. A fake ``rtmidi`` module stands in for the real
ports so this runs with no hardware and no FL; the wire codec itself
(encode_message/decode_frame/Reassembler) is fl-scripts' own frozen surface,
already covered by fl-scripts/controller/tests/test_framing.py.
"""

from __future__ import annotations

import sys
import types

import pytest


class _FakeMidiOut:
    def __init__(self):
        self.sent: list[bytes] = []
        self.port_name = None

    def open_virtual_port(self, name):
        self.port_name = name

    def send_message(self, message):
        self.sent.append(bytes(message))

    def close_port(self):
        pass


class _FakeMidiIn:
    def __init__(self):
        self.callback = None
        self.port_name = None

    def ignore_types(self, **kwargs):
        pass

    def set_callback(self, fn):
        self.callback = fn

    def open_virtual_port(self, name):
        self.port_name = name

    def close_port(self):
        pass


@pytest.fixture
def control(monkeypatch):
    fake_rtmidi = types.ModuleType("rtmidi")
    fake_rtmidi.MidiOut = _FakeMidiOut
    fake_rtmidi.MidiIn = _FakeMidiIn
    monkeypatch.setitem(sys.modules, "rtmidi", fake_rtmidi)

    from fl_mcp import control_midi

    c = control_midi.MidiControl()
    yield c
    c.close()


def _last_sent_request(control) -> dict:
    """Decode the most recent frame the control sent, as the controller
    script would (device_FLMCP's own decode_frame, imported by control_midi)."""
    from fl_mcp.control_midi import decode_frame

    frame = control._out.sent[-1]
    _frame_type, payload = decode_frame(bytes(frame))
    return payload


def _deliver_response(control, req_id: int, **fields) -> None:
    from fl_mcp.control_midi import TYPE_RESPONSE, encode_message

    payload = {"id": req_id, "ok": True, **fields}
    for frame in encode_message(TYPE_RESPONSE, payload, msg_id=req_id):
        control._in.callback((list(frame), 0.0))


def _deliver_event(control, **fields) -> None:
    from fl_mcp.control_midi import TYPE_EVENT, encode_message

    for frame in encode_message(TYPE_EVENT, fields, msg_id=0):
        control._in.callback((list(frame), 0.0))


def test_ports_open_with_the_documented_names(control):
    assert control._out.port_name == "FLMCP Req"
    assert control._in.port_name == "FLMCP Res"


def test_call_round_trips_a_request_and_response(control):
    import threading

    result = {}

    def worker():
        result["resp"] = control.call("ping")

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=0.5)
    assert t.is_alive()  # blocked waiting for the response we haven't sent yet

    sent = _last_sent_request(control)
    assert sent["op"] == "ping"
    _deliver_response(control, sent["id"], ret="pong")
    t.join(timeout=1.0)
    assert result["resp"] == {"id": sent["id"], "ok": True, "ret": "pong"}


def test_call_times_out_when_nothing_answers(control):
    with pytest.raises(TimeoutError):
        control.call("ping", timeout=0.05)


def test_send_request_converts_timeout_to_control_plane_unavailable(control):
    from fl_mcp import errors

    with pytest.raises(errors.ToolError) as exc_info:
        control.send_request("ping", timeout=0.05)
    assert exc_info.value.category == "control_plane_unavailable"


def test_send_request_wraps_the_response_as_a_control_response(control):
    import threading

    from fl_mcp import schemas

    result = {}

    def worker():
        result["resp"] = control.send_request("state.snapshot")

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=0.2)
    assert t.is_alive()  # blocked waiting for the response we haven't sent yet

    sent = _last_sent_request(control)
    assert sent["op"] == "state.snapshot"
    _deliver_response(control, sent["id"], ret={"ppq": 96})
    t.join(timeout=1.0)
    assert isinstance(result["resp"], schemas.ControlResponse)
    assert result["resp"].ok is True
    assert result["resp"].ret == {"ppq": 96}


def test_await_hello_returns_once_the_event_arrives(control):
    _deliver_event(control, op="hello", script_ver="0.1.0", fl_ver="2026.0", ppq=96)
    hello = control.await_hello(timeout=0.5)
    assert hello.fl_ver == "2026.0"
    assert hello.ppq == 96


def test_await_hello_times_out_as_control_plane_unavailable(control):
    from fl_mcp import errors

    with pytest.raises(errors.ToolError) as exc_info:
        control.await_hello(timeout=0.05)
    assert exc_info.value.category == "control_plane_unavailable"


def test_event_listeners_receive_non_hello_events(control):
    seen = []
    control.add_event_listener(seen.append)

    _deliver_event(control, op="dirty_channel", index=1)

    assert seen == [{"op": "dirty_channel", "index": 1}]


def test_ping_true_on_ok_response(control):
    import threading

    result = {}
    t = threading.Thread(target=lambda: result.__setitem__("ok", control.ping(timeout=1.0)))
    t.start()
    t.join(timeout=0.2)
    assert t.is_alive()  # blocked waiting for the response we haven't sent yet

    sent = _last_sent_request(control)
    _deliver_response(control, sent["id"], ret="pong")
    t.join(timeout=1.0)
    assert result["ok"] is True
