"""Pure-Python tests for device_FLMCP.py's framing and dispatch logic.

This is the only part of the controller script we can verify without a
running FL Studio.
"""

from types import SimpleNamespace

import pytest

from fl_mcp.fl_scripts import device_FLMCP as flmcp

# --- frame encode/decode ---------------------------------------------------


def test_encode_decode_round_trip_request():
    payload = {"id": 421, "op": "mixer.setTrackVolume", "args": [4, 0.85], "kwargs": {}}
    frame = flmcp.encode_frame(flmcp.TYPE_REQUEST, payload)
    assert frame[0] == 0xF0 and frame[-1] == 0xF7
    assert tuple(frame[1:4]) == flmcp.MAGIC
    assert all(b <= 0x7F for b in frame[1:-1]), "every byte between F0 and F7 must be 7-bit"
    assert flmcp.decode_frame(frame) == (flmcp.TYPE_REQUEST, payload)


def test_encode_decode_round_trip_response_error():
    payload = {"id": 421, "ok": False, "err": "TypeError: bad arg", "trace": "..."}
    frame = flmcp.encode_frame(flmcp.TYPE_RESPONSE, payload)
    assert flmcp.decode_frame(frame) == (flmcp.TYPE_RESPONSE, payload)


def test_decode_ignores_foreign_sysex():
    foreign = bytes([0xF0, 0x00, 0x20, 0x29, 0x02, 0x01, 0xF7])  # unrelated mfr ID
    assert flmcp.decode_frame(foreign) is None


def test_decode_ignores_non_sysex_bytes():
    assert flmcp.decode_frame([1, 2, 3]) is None


def test_decode_rejects_version_mismatch():
    frame = bytearray(flmcp.encode_frame(flmcp.TYPE_REQUEST, {"id": 1, "op": "ping"}))
    frame[4] = 0x99
    with pytest.raises(flmcp.FrameError, match="version"):
        flmcp.decode_frame(bytes(frame))


def test_decode_rejects_malformed_payload():
    header = bytes([0xF0, *flmcp.MAGIC, flmcp.PROTO_VER, flmcp.TYPE_REQUEST])
    frame = header + b"not-valid-base64!!" + bytes([0xF7])
    with pytest.raises(flmcp.FrameError, match="malformed payload"):
        flmcp.decode_frame(frame)


def test_decode_rejects_non_object_payload():
    frame = flmcp.encode_frame(flmcp.TYPE_REQUEST, [1, 2, 3])
    with pytest.raises(flmcp.FrameError, match="not a JSON object"):
        flmcp.decode_frame(frame)


# --- chunking / reassembly --------------------------------------------------


def test_small_message_is_a_single_frame():
    payload = {"id": 1, "ok": True, "ret": None}
    frames = flmcp.encode_message(flmcp.TYPE_RESPONSE, payload)
    assert len(frames) == 1
    assert flmcp.decode_frame(frames[0]) == (flmcp.TYPE_RESPONSE, payload)


def test_large_message_chunks_and_reassembles():
    payload = {
        "id": 99,
        "ok": True,
        "ret": {"params": [{"idx": i, "name": f"param_{i}", "value": 0.5} for i in range(80)]},
    }
    frames = flmcp.encode_message(flmcp.TYPE_RESPONSE, payload)
    assert len(frames) > 1
    assert all(len(f) <= flmcp.MAX_FRAME_BYTES for f in frames)

    reassembler = flmcp.Reassembler()
    result = None
    for f in frames:
        frame_type, chunk_payload = flmcp.decode_frame(f)
        assert frame_type == flmcp.TYPE_CHUNK
        result = reassembler.feed(frame_type, chunk_payload)
    assert result == payload


def test_reassembler_tolerates_out_of_order_chunks():
    payload = {"id": 7, "data": "x" * 2000}
    frames = flmcp.encode_message(flmcp.TYPE_EVENT, payload, msg_id=7)
    assert len(frames) > 1
    decoded = [flmcp.decode_frame(f) for f in frames]

    reassembler = flmcp.Reassembler()
    result = None
    for frame_type, chunk_payload in reversed(decoded):
        result = reassembler.feed(frame_type, chunk_payload)
    assert result == payload


def test_reassembler_keeps_concurrent_messages_separate():
    a_frames = flmcp.encode_message(flmcp.TYPE_EVENT, {"id": 1, "data": "a" * 1000}, msg_id=1)
    b_frames = flmcp.encode_message(flmcp.TYPE_EVENT, {"id": 2, "data": "b" * 1000}, msg_id=2)
    assert len(a_frames) > 1 and len(b_frames) > 1

    reassembler = flmcp.Reassembler()
    results = {}
    for f in a_frames[:-1] + b_frames:  # interleaved; hold back a's last chunk
        frame_type, payload = flmcp.decode_frame(f)
        out = reassembler.feed(frame_type, payload)
        if out is not None:
            results[out["id"]] = out
    assert results == {2: {"id": 2, "data": "b" * 1000}}

    frame_type, payload = flmcp.decode_frame(a_frames[-1])
    out = reassembler.feed(frame_type, payload)
    results[out["id"]] = out
    assert results[1] == {"id": 1, "data": "a" * 1000}


# --- dispatch ----------------------------------------------------------------


def test_dispatch_ping():
    assert flmcp._dispatch({"id": 1, "op": "ping"}) == {"id": 1, "ok": True, "ret": "pong"}


def test_dispatch_unknown_op():
    result = flmcp._dispatch({"id": 2, "op": "nope.nothing"})
    assert result == {"id": 2, "ok": False, "err": "unknown op 'nope.nothing'"}


def test_dispatch_rejects_unwhitelisted_module():
    result = flmcp._dispatch({"id": 3, "op": "os.system", "args": ["echo hi"]})
    assert result["ok"] is False
    assert "unknown op" in result["err"]


def test_dispatch_rejects_private_names(monkeypatch):
    fake = SimpleNamespace(_secret=lambda: "should not be reachable")
    monkeypatch.setitem(flmcp.ALLOWED, "transport", fake)
    result = flmcp._dispatch({"id": 4, "op": "transport._secret"})
    assert result["ok"] is False
    assert "unknown op" in result["err"]


def test_dispatch_calls_whitelisted_function_with_positional_args(monkeypatch):
    calls = []
    fake_mixer = SimpleNamespace(setTrackVolume=lambda idx, vol: calls.append((idx, vol)))
    monkeypatch.setitem(flmcp.ALLOWED, "mixer", fake_mixer)
    result = flmcp._dispatch({"id": 5, "op": "mixer.setTrackVolume", "args": [4, 0.85]})
    assert result == {"id": 5, "ok": True, "ret": None}
    assert calls == [(4, 0.85)]


def test_dispatch_save_uses_documented_global_transport_command(monkeypatch):
    calls = []
    fake_midi = SimpleNamespace(FPT_Save=92, PME_System=1, GT_All=2)
    fake_transport = SimpleNamespace(
        globalTransport=lambda command, value, pmeflags, flags: calls.append(
            (command, value, pmeflags, flags)
        )
        or 8
    )
    monkeypatch.setattr(flmcp, "midi", fake_midi)
    monkeypatch.setattr(flmcp, "transport", fake_transport)

    assert flmcp._dispatch({"id": 6, "op": "ctl.save"}) == {"id": 6, "ok": True, "ret": 8}
    assert calls == [(fake_midi.FPT_Save, 1, fake_midi.PME_System, fake_midi.GT_All)]


def test_dispatch_captures_exceptions(monkeypatch):
    def boom(*a, **kw):
        raise ValueError("bad param index")

    monkeypatch.setitem(flmcp.ALLOWED, "plugins", SimpleNamespace(setParamValue=boom))
    result = flmcp._dispatch({"id": 6, "op": "plugins.setParamValue", "args": [0.5, 999, 0]})
    assert result["ok"] is False
    assert "ValueError: bad param index" in result["err"]
    assert "trace" in result


def test_dispatch_preserves_generic_plugin_parameter_argument_order(monkeypatch):
    calls = []
    plugin_api = SimpleNamespace(setParamValue=lambda value, index, channel, slot: calls.append(
        (value, index, channel, slot)
    ))
    monkeypatch.setitem(flmcp.ALLOWED, "plugins", plugin_api)

    result = flmcp._dispatch(
        {"id": 7, "op": "plugins.setParamValue", "args": [0.5, 41, 3, -1]}
    )

    assert result == {"id": 7, "ok": True, "ret": None}
    assert calls == [(0.5, 41, 3, -1)]


def test_dispatch_never_echoes_a_non_int_id():
    # ControlResponse.id is a required `int` in schemas.py - a malformed
    # request must not produce a response the server-side model rejects.
    assert flmcp._dispatch({"id": None, "op": "ping"})["id"] == flmcp.UNKNOWN_ID
    assert flmcp._dispatch({"op": "ping"})["id"] == flmcp.UNKNOWN_ID
    assert flmcp._dispatch({"id": "421", "op": "ping"})["id"] == flmcp.UNKNOWN_ID
    assert flmcp._dispatch({"id": 421, "op": "ping"})["id"] == 421


def test_onsysex_reports_unknown_id_for_a_frame_error(monkeypatch):
    sent = []
    monkeypatch.setattr(flmcp, "device", SimpleNamespace(midiOutSysex=lambda frame: sent.append(frame)))
    frame = bytearray(flmcp.encode_frame(flmcp.TYPE_REQUEST, {"id": 1, "op": "ping"}))
    frame[4] = 0x99  # corrupt version byte -> unrecoverable id
    flmcp.OnSysEx(SimpleNamespace(sysex=bytes(frame)))
    assert len(sent) == 1
    frame_type, response = flmcp.decode_frame(sent[0])
    assert frame_type == flmcp.TYPE_RESPONSE
    assert response == {"id": flmcp.UNKNOWN_ID, "ok": False, "err": response["err"]}
    assert isinstance(response["id"], int)


# --- OnIdle deferral for FL's "unsafe at current time" guard ----------------


def test_dispatch_defers_unsafe_at_current_time_and_returns_no_response(monkeypatch):
    monkeypatch.setattr(flmcp, "_deferred_ops", [])
    calls = {"n": 0}

    def flaky(bpm):
        calls["n"] += 1
        raise RuntimeError("Operation unsafe at current time")

    monkeypatch.setitem(flmcp.ALLOWED, "mixer", SimpleNamespace(setCurrentTempo=flaky))
    result = flmcp._dispatch({"id": 50, "op": "mixer.setCurrentTempo", "args": [128.0]})
    assert result is None
    assert calls["n"] == 1
    assert len(flmcp._deferred_ops) == 1
    assert flmcp._deferred_ops[0]["req_id"] == 50


def test_onidle_retries_deferred_op_until_it_lands(monkeypatch):
    monkeypatch.setattr(flmcp, "_deferred_ops", [])
    calls = {"n": 0}

    def flaky(bpm):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Operation unsafe at current time")
        return None

    monkeypatch.setitem(flmcp.ALLOWED, "mixer", SimpleNamespace(setCurrentTempo=flaky))
    sent = []
    monkeypatch.setattr(flmcp, "device", SimpleNamespace(midiOutSysex=lambda frame: sent.append(frame)))

    assert flmcp._dispatch({"id": 51, "op": "mixer.setCurrentTempo", "args": [128.0]}) is None
    assert calls["n"] == 1

    flmcp.OnIdle()  # 2nd attempt - still unsafe
    assert calls["n"] == 2
    assert sent == []
    assert len(flmcp._deferred_ops) == 1

    flmcp.OnIdle()  # 3rd attempt - succeeds
    assert calls["n"] == 3
    assert flmcp._deferred_ops == []
    assert len(sent) == 1
    frame_type, response = flmcp.decode_frame(sent[0])
    assert frame_type == flmcp.TYPE_RESPONSE
    assert response == {"id": 51, "ok": True, "ret": None}


def test_onidle_gives_up_after_max_ticks_and_replies_with_the_error(monkeypatch):
    monkeypatch.setattr(flmcp, "_deferred_ops", [])

    def always_unsafe(*a, **kw):
        raise RuntimeError("Operation unsafe at current time: mid-cycle")

    monkeypatch.setitem(flmcp.ALLOWED, "mixer", SimpleNamespace(setCurrentTempo=always_unsafe))
    sent = []
    monkeypatch.setattr(flmcp, "device", SimpleNamespace(midiOutSysex=lambda frame: sent.append(frame)))

    assert flmcp._dispatch({"id": 52, "op": "mixer.setCurrentTempo", "args": [128.0]}) is None
    for _ in range(flmcp.DEFER_MAX_TICKS - 1):
        flmcp.OnIdle()
        assert sent == []
        assert len(flmcp._deferred_ops) == 1

    flmcp.OnIdle()  # final tick - gives up
    assert flmcp._deferred_ops == []
    assert len(sent) >= 1  # the error response carries a trace, so it may chunk

    reassembler = flmcp.Reassembler()
    response = None
    for frame in sent:
        frame_type, chunk_payload = flmcp.decode_frame(frame)
        response = reassembler.feed(frame_type, chunk_payload) or response
    assert response["id"] == 52
    assert response["ok"] is False
    assert "unsafe at current time" in response["err"]


def test_dispatch_does_not_defer_unrelated_runtime_errors(monkeypatch):
    monkeypatch.setattr(flmcp, "_deferred_ops", [])

    def boom(*a, **kw):
        raise RuntimeError("some other FL runtime error")

    monkeypatch.setitem(flmcp.ALLOWED, "mixer", SimpleNamespace(setCurrentTempo=boom))
    result = flmcp._dispatch({"id": 53, "op": "mixer.setCurrentTempo", "args": [128.0]})
    assert result["ok"] is False
    assert "some other FL runtime error" in result["err"]
    assert flmcp._deferred_ops == []


def test_onsysex_sends_no_response_when_op_is_deferred(monkeypatch):
    monkeypatch.setattr(flmcp, "_deferred_ops", [])

    def flaky(*a, **kw):
        raise RuntimeError("Operation unsafe at current time")

    monkeypatch.setitem(flmcp.ALLOWED, "mixer", SimpleNamespace(setCurrentTempo=flaky))
    sent = []
    monkeypatch.setattr(flmcp, "device", SimpleNamespace(midiOutSysex=lambda frame: sent.append(frame)))
    frame = flmcp.encode_frame(
        flmcp.TYPE_REQUEST, {"id": 54, "op": "mixer.setCurrentTempo", "args": [128.0], "kwargs": {}}
    )
    flmcp.OnSysEx(SimpleNamespace(sysex=frame))
    assert sent == []
    assert len(flmcp._deferred_ops) == 1
    assert flmcp._deferred_ops[0]["req_id"] == 54


# --- composite ops -------------------------------------------------------


def test_state_snapshot_composite_op(monkeypatch):
    monkeypatch.setattr(flmcp, "general", SimpleNamespace(getRecPPQ=lambda: 96))
    monkeypatch.setattr(
        flmcp,
        "mixer",
        SimpleNamespace(
            getCurrentTempo=lambda: 140.0,
            trackCount=lambda: 1,
            getTrackName=lambda i: "Master",
            getTrackVolume=lambda i: 0.78,
            getTrackPan=lambda i: 0.5,
            isTrackMuted=lambda i: False,
            isTrackSolo=lambda i: False,
        ),
    )
    monkeypatch.setattr(flmcp, "transport", SimpleNamespace(isPlaying=lambda: True, getSongPos=lambda: 0.0))
    monkeypatch.setattr(flmcp, "patterns", SimpleNamespace(patternNumber=lambda: 1, patternCount=lambda: 3))
    monkeypatch.setattr(
        flmcp,
        "channels",
        SimpleNamespace(
            channelCount=lambda: 1,
            getChannelName=lambda i: "Bass",
            isChannelMuted=lambda i: False,
            getChannelVolume=lambda i: 0.8,
            getChannelPan=lambda i: 0.5,
            getTargetFxTrack=lambda i: 0,
        ),
    )

    snap = flmcp._dispatch({"id": 8, "op": "state.snapshot"})
    assert snap["ok"] is True
    assert snap["ret"]["ppq"] == 96
    assert snap["ret"]["tempo"] == 140.0
    assert snap["ret"]["channels"] == [
        {"idx": 0, "name": "Bass", "muted": False, "vol": 0.8, "pan": 0.5, "target_mixer_track": 0}
    ]
    assert snap["ret"]["mixer"] == [
        {"idx": 0, "name": "Master", "vol": 0.78, "pan": 0.5, "muted": False, "solo": False}
    ]


def test_undo_probe_composite_op(monkeypatch):
    monkeypatch.setattr(
        flmcp,
        "general",
        SimpleNamespace(
            getUndoHistoryPos=lambda: 5,
            getUndoHistoryCount=lambda: 5,
            getUndoHistoryLast=lambda: 0,
            getUndoLevelHint=lambda: "1/5",
        ),
    )
    result = flmcp._dispatch({"id": 9, "op": "state.undo_probe"})
    assert result == {
        "id": 9,
        "ok": True,
        "ret": {"history_pos": 5, "history_count": 5, "history_last": 0, "history_hint": "1/5"},
    }


def test_unknown_composite_op():
    result = flmcp._dispatch({"id": 10, "op": "state.does_not_exist"})
    assert result == {"id": 10, "ok": False, "err": "unknown composite op 'state.does_not_exist'"}


def test_unknown_mcp_composite_op():
    result = flmcp._dispatch({"id": 11, "op": "mcp.does_not_exist"})
    assert result == {"id": 11, "ok": False, "err": "unknown composite op 'mcp.does_not_exist'"}


def test_select_channels_selects_exactly_one_channel(monkeypatch):
    selected = []
    monkeypatch.setattr(flmcp, "channels", SimpleNamespace(selectOneChannel=selected.append))

    result = flmcp._dispatch({"id": 12, "op": "select_channels", "args": [[4]]})

    assert result == {"id": 12, "ok": True, "ret": 4}
    assert selected == [4]


# --- MCPTools bridge -------------------------------------------------------


def test_mcp_import_tools_reports_every_candidate_dir_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(flmcp, "_DEFAULT_MCP_TOOLS_DIRS", [str(tmp_path / "nope-a"), str(tmp_path / "nope-b")])
    monkeypatch.setitem(flmcp._mcp_state, "module", None)
    result = flmcp._dispatch({"id": 20, "op": "mcp.import_tools"})
    assert result["ok"] is True
    ret = result["ret"]
    assert ret["imported"] is False
    assert "nope-a" in ret["err"] and "nope-b" in ret["err"]
    assert ret["whitelist"] == sorted(flmcp.MCP_TOOL_WHITELIST)


def test_mcp_import_tools_succeeds_against_a_stub_module(monkeypatch, tmp_path):
    (tmp_path / "MCPTools.py").write_text(
        "def get_session_context():\n    return {}\n"
        "def add_effect(plugin, target_tracks, slot_number):\n    return True\n"
    )
    monkeypatch.setitem(flmcp._mcp_state, "module", None)
    result = flmcp._dispatch({"id": 21, "op": "mcp.import_tools", "kwargs": {"tools_dir": str(tmp_path)}})
    assert result["ok"] is True
    ret = result["ret"]
    assert ret["imported"] is True
    assert ret["tools_dir"] == str(tmp_path)
    assert "get_session_context" in ret["available"]
    assert "add_effect" in ret["available"]
    assert "add_channel" in ret["missing"]  # whitelisted but not defined on our stub


def test_mcp_call_requires_a_function_name():
    result = flmcp._dispatch({"id": 22, "op": "mcp.call"})
    assert result["ok"] is False
    assert "requires the MCPTools function name" in result["err"]


def test_mcp_call_rejects_names_outside_the_whitelist(monkeypatch):
    monkeypatch.setitem(flmcp._mcp_state, "module", SimpleNamespace(os_system=lambda *a: None))
    result = flmcp._dispatch({"id": 23, "op": "mcp.call", "args": ["os_system", "echo hi"]})
    assert result["ok"] is False
    assert "not a whitelisted MCPTools function" in result["err"]


def test_mcp_call_requires_import_first(monkeypatch):
    monkeypatch.setitem(flmcp._mcp_state, "module", None)
    result = flmcp._dispatch({"id": 24, "op": "mcp.call", "args": ["get_session_context"]})
    assert result["ok"] is False
    assert "call mcp.import_tools first" in result["err"]


def test_mcp_call_dispatches_positional_and_keyword_args(monkeypatch):
    calls = []
    fake_module = SimpleNamespace(
        add_effect=lambda plugin, target_tracks, slot_number=1: calls.append((plugin, target_tracks, slot_number))
    )
    monkeypatch.setitem(flmcp._mcp_state, "module", fake_module)
    result = flmcp._dispatch(
        {"id": 25, "op": "mcp.call", "args": ["add_effect", "Fruity Limiter", "10"], "kwargs": {"slot_number": 1}}
    )
    assert result == {"id": 25, "ok": True, "ret": None}
    assert calls == [("Fruity Limiter", "10", 1)]


def test_mcp_call_captures_exceptions_from_the_whitelisted_function(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("FL says no")

    monkeypatch.setitem(flmcp._mcp_state, "module", SimpleNamespace(get_tempo=boom))
    result = flmcp._dispatch({"id": 26, "op": "mcp.call", "args": ["get_tempo"]})
    assert result["ok"] is False
    assert "RuntimeError: FL says no" in result["err"]


def test_ctl_hello_answers_the_same_fields_the_wire_pushes(monkeypatch):
    monkeypatch.setattr(flmcp, "ui", SimpleNamespace(getVersion=lambda: "26.1.5"))
    monkeypatch.setattr(flmcp, "general", SimpleNamespace(getRecPPQ=lambda: 96))
    monkeypatch.setattr(flmcp, "mixer", SimpleNamespace(getCurrentTempo=lambda: 120.0))
    response = flmcp._dispatch({"id": 904, "op": "ctl.hello"})
    assert response["ok"] is True
    assert set(response["ret"]) >= {"script_ver", "fl_ver", "ppq"}
