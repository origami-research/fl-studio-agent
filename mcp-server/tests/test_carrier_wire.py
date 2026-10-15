"""The MIDI carrier: correlation honesty, dropped-frame accounting, hello
tracking, and event dispatch - against a fake ``rtmidi`` module, no hardware,
no FL. The codec itself is flmcp_codec's frozen surface, covered by
tests/test_controller_framing.py.
"""

from __future__ import annotations

import sys
import threading
import types
from typing import Any

import pytest

from fl_mcp import errors
from fl_mcp.core.carriers import wire
from fl_mcp.fl_scripts.flmcp_codec import (
    MAGIC,
    TYPE_EVENT,
    TYPE_RESPONSE,
    decode_frame,
    encode_frame,
    encode_message,
)
from fl_mcp.schemas import ControlRequest, ControlResponse


class _FakeMidiOut:
    def __init__(self):
        self.sent: list[bytes] = []
        self.port_name = None

    def open_virtual_port(self, name):
        self.port_name = name

    def get_ports(self):
        return [wire.REQ_PORT_NAME]

    def open_port(self, number, name=None):
        self.port_name = self.get_ports()[number]

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

    def get_ports(self):
        return [wire.RES_PORT_NAME]

    def open_port(self, number, name=None):
        self.port_name = self.get_ports()[number]

    def close_port(self):
        pass

    def deliver(self, frame: bytes) -> None:
        """Hand one raw frame to the registered callback the way real rtmidi
        does: fn((message_bytes, delta_time), user_data)."""
        self.callback((list(frame), 0.0), None)


class _FakeRtmidiError(Exception):
    pass


def _fake_rtmidi_module() -> types.ModuleType:
    mod = types.ModuleType("rtmidi")
    created: dict[str, Any] = {}

    def midi_out():
        created["out"] = _FakeMidiOut()
        return created["out"]

    def midi_in():
        created["in"] = _FakeMidiIn()
        return created["in"]

    mod.MidiOut = midi_out
    mod.MidiIn = midi_in
    mod.RtMidiError = _FakeRtmidiError
    mod.created = created
    return mod


@pytest.fixture
def rig(monkeypatch):
    """A MidiControl wired to fake ports, plus the fake ports themselves."""
    mod = _fake_rtmidi_module()
    monkeypatch.setitem(sys.modules, "rtmidi", mod)
    control = wire.MidiControl()
    yield control, mod.created["in"], mod.created["out"]
    control.close()


def _last_sent_request(out: _FakeMidiOut) -> dict:
    decoded = decode_frame(out.sent[-1])
    assert decoded is not None
    _frame_type, payload = decoded
    return payload


def _deliver_response(midi_in: _FakeMidiIn, req_id: int, **fields) -> None:
    payload = {"id": req_id, "ok": True, **fields}
    for frame in encode_message(TYPE_RESPONSE, payload, msg_id=req_id):
        midi_in.deliver(frame)


def _deliver_event(midi_in: _FakeMidiIn, **fields) -> None:
    for frame in encode_message(TYPE_EVENT, fields, msg_id=0):
        midi_in.deliver(frame)


def _in_thread(fn) -> tuple[threading.Thread, dict]:
    """Run fn on a worker; the box collects its result or exception."""
    box: dict[str, Any] = {}

    def worker():
        try:
            box["result"] = fn()
        except Exception as exc:
            box["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    return t, box


def test_ports_open_with_the_documented_names(rig):
    _control, midi_in, midi_out = rig
    assert midi_out.port_name == wire.REQ_PORT_NAME
    assert midi_in.port_name == wire.RES_PORT_NAME


def test_windows_opens_provisioned_ports_by_exact_name(monkeypatch):
    mod = _fake_rtmidi_module()
    monkeypatch.setitem(sys.modules, "rtmidi", mod)
    control = wire.windows_midi_control()
    try:
        assert mod.created["out"].port_name == wire.REQ_PORT_NAME
        assert mod.created["in"].port_name == wire.RES_PORT_NAME
    finally:
        control.close()


def _listed_ports(out_names, in_names):
    """A fake rtmidi whose ports list the given names, as a real backend would."""
    mod = _fake_rtmidi_module()
    make_out, make_in = mod.MidiOut, mod.MidiIn

    def midi_out():
        port = make_out()
        port.get_ports = lambda: list(out_names)
        return port

    def midi_in():
        port = make_in()
        port.get_ports = lambda: list(in_names)
        return port

    mod.MidiOut, mod.MidiIn = midi_out, midi_in
    return mod


def test_windows_opens_winmm_suffixed_loopmidi_names(monkeypatch):
    """RtMidi's WinMM backend lists ports as '<name> <index>' (RtMidi.cpp
    getPortName). Exact-name lookup alone never found loopMIDI's ports."""
    mod = _listed_ports(["Microsoft GS Wavetable Synth 0", "FLMCP Req 1", "FLMCP Res 2"], ["FLMCP Req 0", "FLMCP Res 1"])
    monkeypatch.setitem(sys.modules, "rtmidi", mod)
    control = wire.windows_midi_control()
    try:
        assert mod.created["out"].port_name == "FLMCP Req 1"
        assert mod.created["in"].port_name == "FLMCP Res 1"
    finally:
        control.close()


@pytest.mark.parametrize(
    ("listed", "problem"),
    [(["FLMCP Request 0", "FLMCP Res 1"], "is missing"), (["FLMCP Req 0", "FLMCP Req 1"], "more than once")],
)
def test_windows_refuses_a_missing_or_ambiguous_port(monkeypatch, listed, problem):
    monkeypatch.setitem(sys.modules, "rtmidi", _listed_ports(listed, ["FLMCP Res 0"]))
    with pytest.raises(wire.ControlUnavailable, match=problem):
        wire.windows_midi_control()


def test_missing_rtmidi_is_control_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "rtmidi", None)  # import raises ImportError
    with pytest.raises(wire.ControlUnavailable):
        wire.MidiControl()


def test_port_open_failure_is_control_unavailable(monkeypatch):
    mod = _fake_rtmidi_module()

    def broken_out():
        raise _FakeRtmidiError("no CoreMIDI")

    mod.MidiOut = broken_out
    monkeypatch.setitem(sys.modules, "rtmidi", mod)
    with pytest.raises(wire.ControlUnavailable):
        wire.MidiControl()


def test_is_live_tracks_the_hello(rig):
    control, midi_in, _midi_out = rig
    assert control.is_live() is False  # plugged in, nobody listening
    _deliver_event(midi_in, op="hello", script_ver="0.1.0", fl_ver="2026.0", ppq=96)
    assert control.is_live() is True


def test_carrier_shape_send_then_await(rig):
    """The staged halves send_request composes: dispatch, then correlate."""
    control, midi_in, midi_out = rig
    control.send(ControlRequest(id=1, op="state.snapshot"))
    assert _last_sent_request(midi_out)["op"] == "state.snapshot"
    _deliver_response(midi_in, 1, ret={"ppq": 96})
    assert control.await_response(1, timeout=1.0).ret == {"ppq": 96}


def test_plugin_parameter_request_preserves_fl_generic_argument_order(rig):
    control, _midi_in, midi_out = rig
    control.send(ControlRequest(id=7, op="plugins.setParamValue", args=[0.5, 41, 3, -1]))

    assert _last_sent_request(midi_out) == {
        "id": 7,
        "op": "plugins.setParamValue",
        "args": [0.5, 41, 3, -1],
        "kwargs": {},
        "pid": None,  # untagged = broadcast; a session-scoped carrier sets this
    }


def test_send_request_round_trips_and_validates(rig):
    control, midi_in, midi_out = rig
    t, box = _in_thread(lambda: control.send_request("state.snapshot", timeout=2.0))
    t.join(timeout=0.3)
    assert t.is_alive()  # blocked waiting on the response we haven't sent yet

    sent = _last_sent_request(midi_out)
    assert sent["op"] == "state.snapshot"
    _deliver_response(midi_in, sent["id"], ret={"ppq": 96})
    t.join(timeout=1.0)
    resp = box["result"]
    assert isinstance(resp, ControlResponse)
    assert resp.ok is True
    assert resp.ret == {"ppq": 96}


def test_send_request_timeout_is_control_plane_unavailable(rig):
    control, _midi_in, _midi_out = rig
    with pytest.raises(errors.ToolError) as exc_info:
        control.send_request("ping", timeout=0.05)
    assert exc_info.value.category == "control_plane_unavailable"
    # No hello ever arrived: the language must say "not attached", not "busy".
    assert "attached" not in exc_info.value.message


def test_timeout_after_hello_reads_as_busy_not_absent(rig):
    """docs/21 A6: FL parks the first engine call after a load as 'unsafe at
    current time' and the controller gives up first. With a hello on record,
    a timeout must not claim the controller is missing."""
    control, midi_in, _midi_out = rig
    _deliver_event(midi_in, op="hello", script_ver="0.1.0", fl_ver="2026.0", ppq=96)
    with pytest.raises(errors.ToolError) as exc_info:
        control.send_request("mixer.getCurrentTempo", timeout=0.05)
    err = exc_info.value
    assert err.category == "control_plane_unavailable"
    assert err.retryable is True
    assert "attached" in err.message and "busy" in err.message


def test_response_with_wrong_id_never_satisfies_the_call(rig):
    control, midi_in, _midi_out = rig
    _deliver_response(midi_in, 999, ret="for nobody")
    with pytest.raises(errors.ToolError):
        control.send_request("ping", timeout=0.05)


def test_late_response_to_an_abandoned_call_is_discarded(rig):
    control, midi_in, midi_out = rig
    with pytest.raises(errors.ToolError):
        control.send_request("ping", timeout=0.05)
    abandoned = _last_sent_request(midi_out)

    # The response lands after the caller gave up: it must not leak anywhere.
    _deliver_response(midi_in, abandoned["id"], ret="too late")

    t, box = _in_thread(lambda: control.send_request("ping", timeout=1.0))
    t.join(timeout=0.2)
    assert t.is_alive()  # the orphan did not satisfy the new call
    fresh = _last_sent_request(midi_out)
    assert fresh["id"] != abandoned["id"]
    _deliver_response(midi_in, fresh["id"], ret="pong")
    t.join(timeout=1.0)
    assert box["result"].ret == "pong"


def test_chunked_response_is_correlated_not_dropped(rig):
    """The controller sends every response through encode_message, so a large
    response arrives as TYPE_CHUNK frames. v1 routed by frame type only and
    silently dropped these; the port classifies the reassembled payload."""
    control, midi_in, midi_out = rig
    t, box = _in_thread(lambda: control.send_request("state.snapshot", timeout=2.0))
    t.join(timeout=0.3)
    assert t.is_alive()

    sent = _last_sent_request(midi_out)
    big = "x" * 2000  # forces chunking (single frame would exceed the 512 cap)
    frames = list(
        encode_message(TYPE_RESPONSE, {"id": sent["id"], "ok": True, "ret": big}, msg_id=sent["id"])
    )
    assert len(frames) > 1  # the premise: this response really is chunked
    for frame in frames:
        midi_in.deliver(frame)
    t.join(timeout=1.0)
    assert box["result"].ret == big
    assert control.dropped_frames == 0


def test_chunked_event_still_routes_as_an_event(rig):
    control, midi_in, _midi_out = rig
    seen: list[dict] = []
    control.add_event_listener(seen.append)
    fields = {"op": "refresh", "detail": "y" * 2000}
    frames = list(encode_message(TYPE_EVENT, fields, msg_id=7))
    assert len(frames) > 1
    for frame in frames:
        midi_in.deliver(frame)
    assert seen == [fields]
    assert control.dropped_frames == 0  # valid chunk frames are never "dropped"


def test_await_hello_returns_once_the_event_arrives(rig):
    control, midi_in, _midi_out = rig
    _deliver_event(midi_in, op="hello", script_ver="0.1.0", fl_ver="2026.0", ppq=96)
    hello = control.await_hello(timeout=0.5)
    assert hello.fl_ver == "2026.0"
    assert hello.ppq == 96


def test_await_hello_times_out_as_control_plane_unavailable(rig):
    control, _midi_in, _midi_out = rig
    with pytest.raises(errors.ToolError) as exc_info:
        control.await_hello(timeout=0.05)
    assert exc_info.value.category == "control_plane_unavailable"


def test_malformed_hello_counts_as_dropped_and_does_not_unblock(rig):
    control, midi_in, _midi_out = rig
    _deliver_event(midi_in, op="hello", script_ver="0.1.0")  # missing fl_ver/ppq
    assert control.dropped_frames == 1
    with pytest.raises(errors.ToolError):
        control.await_hello(timeout=0.05)


def test_event_listeners_receive_non_hello_events_and_drain_clears(rig):
    control, midi_in, _midi_out = rig
    seen: list[dict] = []
    control.add_event_listener(seen.append)
    _deliver_event(midi_in, op="dirty_channel", index=1)
    assert seen == [{"op": "dirty_channel", "index": 1}]
    assert control.drain_events() == [{"op": "dirty_channel", "index": 1}]
    assert control.drain_events() == []


def test_event_buffer_is_bounded_when_no_consumer_drains_it(rig):
    control, midi_in, _midi_out = rig

    for index in range(wire.EVENT_BUFFER_SIZE + 1):
        _deliver_event(midi_in, op="dirty_channel", index=index)

    events = control.drain_events()
    assert len(events) == wire.EVENT_BUFFER_SIZE
    assert events[0]["index"] == 1


def test_listener_exception_is_contained_and_later_listeners_still_run(rig):
    control, midi_in, _midi_out = rig
    seen: list[dict] = []

    def bad_listener(_event):
        raise RuntimeError("listener bug")

    control.add_event_listener(bad_listener)
    control.add_event_listener(seen.append)
    _deliver_event(midi_in, op="refresh")
    assert seen == [{"op": "refresh"}]
    assert control.dropped_frames == 0  # a listener bug is not a wire problem


def test_dropped_frames_counts_malformed_frames_only(rig):
    control, midi_in, _midi_out = rig
    assert control.dropped_frames == 0

    # Not our traffic (wrong magic): ignored, not counted.
    midi_in.deliver(bytes([0xF0, 0x01, 0x02, 0x03, 0x01, 0x01, 0x41, 0xF7]))
    assert control.dropped_frames == 0

    # Our magic, garbage payload: counted.
    header = bytes([0xF0, MAGIC[0], MAGIC[1], MAGIC[2], 1, TYPE_RESPONSE])
    midi_in.deliver(header + b"%%%not-base64%%%" + bytes([0xF7]))
    assert control.dropped_frames == 1

    # Our magic, unsupported protocol version: counted.
    bad_ver = encode_frame(TYPE_RESPONSE, {"id": 1, "ok": True})
    midi_in.deliver(bad_ver[:4] + bytes([99]) + bad_ver[5:])
    assert control.dropped_frames == 2

    # Well-formed traffic afterwards still lands untouched.
    _deliver_event(midi_in, op="refresh")
    assert control.drain_events() == [{"op": "refresh"}]
    assert control.dropped_frames == 2


def test_close_is_idempotent(rig):
    control, _midi_in, _midi_out = rig
    control.close()
    control.close()


def test_send_request_fails_fast_when_the_shared_wire_stays_busy(rig):
    control, _midi_in, _midi_out = rig
    # Hold the call lock as another session's slow op would; a second request
    # must fail retryably within its own timeout, not queue without bound.
    control._call_lock.acquire()
    try:
        with pytest.raises(errors.ToolError) as exc_info:
            control.send_request("mixer.getCurrentTempo", timeout=0.2)
    finally:
        control._call_lock.release()
    assert exc_info.value.category == "control_plane_busy"
    assert exc_info.value.retryable is True
    assert "busy" in exc_info.value.message


def test_provisioned_ports_ask_the_controller_for_its_hello(monkeypatch):
    """loopMIDI ports outlive FL's controller init: its OnInit hello went out
    before this process opened them, so a provisioned wire asks with ctl.hello
    instead of waiting for an announcement that already happened."""
    mod = _fake_rtmidi_module()
    monkeypatch.setitem(sys.modules, "rtmidi", mod)
    control = wire.windows_midi_control()
    midi_in, midi_out = mod.created["in"], mod.created["out"]
    try:
        thread, box = _in_thread(lambda: control.await_hello(timeout=2.0))
        for _ in range(400):
            if midi_out.sent:
                break
            threading.Event().wait(0.005)
        assert midi_out.sent, "no request was sent"
        request = _last_sent_request(midi_out)
        assert request["op"] == "ctl.hello"
        _deliver_response(midi_in, request["id"], ret={"script_ver": "0.2.1", "fl_ver": "Trial v26.1.6", "ppq": 96})
        thread.join(timeout=2.0)
        assert box["result"].fl_ver == "Trial v26.1.6"
        assert control.is_live()
    finally:
        control.close()


def test_virtual_ports_wait_for_the_announced_hello(rig):
    """Virtual ports hot-plug into a running FL, whose bind announces hello:
    nothing to ask, so nothing is sent."""
    control, _midi_in, midi_out = rig
    with pytest.raises(errors.ToolError):
        control.await_hello(timeout=0.05)
    assert midi_out.sent == []
