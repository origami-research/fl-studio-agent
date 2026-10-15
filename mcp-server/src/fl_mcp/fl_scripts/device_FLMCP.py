# name=FLMCP Bridge Script
# receiveFrom=FLMCP Req
"""FL MCP - control-plane bridge. Runs inside FL Studio's embedded Python 3.9.

Thin, dumb executor: decode a SysEx frame -> call the named FL API function ->
encode the result back out. All intelligence (which op to call, how to
interpret the result) lives in the MCP server; see docs/05-controller-script.md
and docs/03-protocols.md (Protocol A) for the wire spec this implements.

Every FL API call below is checked against the IL-Group FL Studio API Stubs
(github.com/IL-Group/FL-Studio-API-Stubs, `src/midi_controller_scripting/`)
during Spike 3. Corrections against docs/05's draft table are called out
inline as `# Spike 3:`.

This file is imported standalone (no FL modules present) by the pytest suite
(mcp-server/tests/test_controller_framing.py), so every FL-only name is imported through
a guarded block below and the pure framing/dispatch functions never touch it
at import time.
"""

import importlib
import os
import sys
import traceback

try:
    # Real FL modules; only importable from inside FL's embedded interpreter.
    import arrangement
    import channels
    import device
    import general
    import midi
    import mixer
    import patterns
    import playlist
    import plugins
    import transport
    import ui
except ImportError:
    # Outside FL (pytest, the host probe script): framing/dispatch logic below
    # is still fully importable and testable; only OnInit/OnSysEx/etc need FL.
    arrangement = channels = device = general = midi = None
    mixer = patterns = playlist = plugins = transport = ui = None

SCRIPT_VER = "0.2.1"

# ---------------------------------------------------------------------------
# Wire codec (framing, chunking/reassembly) - flmcp_codec.py beside this file is the single
# source of truth (docs/15 fix item 6); embedded verbatim below so this file
# stays a standalone, stdlib-only script FL can load with one file. Do not
# hand-edit the block between the markers - edit flmcp_codec.py and copy its
# contents in. tests/test_codec_sync.py fails if the two ever drift.
# ---------------------------------------------------------------------------
# === BEGIN flmcp_codec.py (embedded verbatim) ===
"""FLMCP wire codec - SysEx framing, chunking/reassembly, encode/decode.

Single source of truth for the wire format shared by the FL controller script
(fl_mcp/fl_scripts/device_FLMCP.py, which embeds this file verbatim
between BEGIN/END markers so it stays a standalone, stdlib-only script FL can
load) and the MCP server (fl_mcp/core/carriers/wire.py, which
imports this module directly). tests/test_codec_sync.py keeps the two copies
from drifting.
"""

import base64
import json

PROTO_VER = 1
MAGIC = (0x7D, 0x4D, 0x43)  # 0x7D (non-commercial/prototyping mfr ID) + "MC"
FRAME_START = 0xF0
FRAME_END = 0xF7

TYPE_REQUEST = 0x01
TYPE_RESPONSE = 0x02
TYPE_EVENT = 0x03
TYPE_CHUNK = 0x04

# SysEx frames are capped at 512 bytes (docs/03) - driver behavior on larger
# frames is unverified and out of scope here [ASSUMED cap holds].
MAX_FRAME_BYTES = 512
# base64 chars of the real payload carried per chunk frame. Comfortably under
# the cap after the chunk-envelope JSON scaffolding and the frame header are
# added back on: (300 + ~40 envelope chars) * 4/3 (base64) + 7 (frame header)
# ~= 460 bytes, verified empirically in tests/test_framing.py.
CHUNK_DATA_CHARS = 300


class FrameError(Exception):
    """Our traffic (magic matched), but malformed: bad version or payload."""


def encode_frame(frame_type, payload_obj):
    """One SysEx frame (bytes, F0..F7 inclusive) for a payload that already
    fits under MAX_FRAME_BYTES. Callers that might exceed the cap should go
    through encode_message instead."""
    json_bytes = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(json_bytes)
    header = bytes([FRAME_START, MAGIC[0], MAGIC[1], MAGIC[2], PROTO_VER, frame_type])
    return header + b64 + bytes([FRAME_END])


def encode_message(frame_type, payload_obj, msg_id=None, max_bytes=MAX_FRAME_BYTES):
    """Every frame needed to send payload_obj, chunking into TYPE_CHUNK frames
    if the single-frame encoding would exceed max_bytes.

    Chunk envelopes carry a slice of the *base64* payload text (not raw JSON),
    so reassembly is one decode step regardless of whether chunking happened.
    """
    single = encode_frame(frame_type, payload_obj)
    if len(single) <= max_bytes:
        return [single]

    json_bytes = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    b64_str = base64.b64encode(json_bytes).decode("ascii")
    if msg_id is None:
        msg_id = payload_obj.get("id", 0)
    pieces = [b64_str[i : i + CHUNK_DATA_CHARS] for i in range(0, len(b64_str), CHUNK_DATA_CHARS)]
    frames = []
    for seq, piece in enumerate(pieces):
        envelope = {"id": msg_id, "seq": seq, "of": len(pieces), "data": piece}
        frames.append(encode_frame(TYPE_CHUNK, envelope))
    return frames


def decode_frame(sysex):
    """Parse one raw SysEx frame.

    Returns ``(frame_type, payload_dict)``, or ``None`` if this isn't our
    traffic at all (wrong envelope shape or magic - some other device sharing
    the port). Raises FrameError if the magic matches but the frame is
    otherwise broken (wrong version, bad base64/JSON) - that case is ours to
    report an error for, not silently ignore.
    """
    b = bytes(sysex)
    if len(b) < 7 or b[0] != FRAME_START or b[-1] != FRAME_END:
        return None
    if tuple(b[1:4]) != MAGIC:
        return None
    ver = b[4]
    frame_type = b[5]
    if ver != PROTO_VER:
        raise FrameError(f"unsupported protocol version {ver} (script speaks {PROTO_VER})")
    body = b[6:-1]
    try:
        json_bytes = base64.b64decode(bytes(body))
        payload = json.loads(json_bytes.decode("utf-8"))
    except Exception as exc:
        raise FrameError(f"malformed payload: {type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FrameError(f"payload is not a JSON object: {type(payload).__name__}")
    return frame_type, payload


class Reassembler:
    """Buffers TYPE_CHUNK frames by message id until all pieces arrive.

    Feed every decoded (frame_type, payload) here. Non-chunk frames pass
    through unchanged (returned immediately); chunk frames return None until
    the last piece lands, then return the reassembled payload dict.
    """

    def __init__(self):
        self._pending = {}  # msg_id -> {seq: data_piece}

    def feed(self, frame_type, payload):
        if frame_type != TYPE_CHUNK:
            return payload
        msg_id, seq, of, data = payload["id"], payload["seq"], payload["of"], payload["data"]
        parts = self._pending.setdefault(msg_id, {})
        parts[seq] = data
        if len(parts) < of:
            return None
        del self._pending[msg_id]
        b64_str = "".join(parts[i] for i in range(of))
        json_bytes = base64.b64decode(b64_str.encode("ascii"))
        return json.loads(json_bytes.decode("utf-8"))
# === END flmcp_codec.py ===

# ControlResponse.id is a required `int` (not Optional) in schemas.py. A frame
# corrupted badly enough that we can't recover the sender's id (bad version,
# unparseable base64/JSON, or a request missing/lying about "id") still needs
# a schema-valid id to respond with. -1 is outside the server's
# "monotonically increasing per session" id range (docs/03), reserved here to
# mean "framing-level error, not correlated to any request."
UNKNOWN_ID = -1


# ---------------------------------------------------------------------------
# Dispatch - whitelist of FL modules, table lookup only, never getattr on
# attacker-controlled strings against builtins/globals. docs/05.
# ---------------------------------------------------------------------------

ALLOWED = {
    "transport": transport,
    "mixer": mixer,
    "channels": channels,
    "patterns": patterns,
    "playlist": playlist,
    "plugins": plugins,
    "ui": ui,
    "general": general,
    "arrangement": arrangement,
}


def _state_snapshot():
    """`state.snapshot` - the one composite read the server calls most; see
    docs/11 §"Composite ops"."""
    return {
        "ppq": general.getRecPPQ(),
        "tempo": mixer.getCurrentTempo(),
        "playing": bool(transport.isPlaying()),
        "song_pos": transport.getSongPos(),
        "current_pattern": patterns.patternNumber(),
        "pattern_count": patterns.patternCount(),
        "channels": [
            {
                "idx": i,
                "name": channels.getChannelName(i),
                "muted": bool(channels.isChannelMuted(i)),
                "vol": channels.getChannelVolume(i),
                "pan": channels.getChannelPan(i),
                "target_mixer_track": channels.getTargetFxTrack(i),
            }
            for i in range(channels.channelCount())
        ],
        "mixer": [
            {
                "idx": i,
                "name": mixer.getTrackName(i),
                "vol": mixer.getTrackVolume(i),
                "pan": mixer.getTrackPan(i),
                "muted": bool(mixer.isTrackMuted(i)),
                "solo": bool(mixer.isTrackSolo(i)),
            }
            for i in range(mixer.trackCount())
        ],
    }


def _undo_probe():
    """`state.undo_probe` - every undo-history *getter* FL exposes, in one
    round trip. Roadmap open question 17 (docs/08): can this attribute whose
    step is on top? Short answer: no, not by content. FL gives
    position/count, never the name string passed to saveUndo() back out.
    """
    return {
        "history_pos": general.getUndoHistoryPos(),
        "history_count": general.getUndoHistoryCount(),
        "history_last": general.getUndoHistoryLast(),
        "history_hint": general.getUndoLevelHint(),
    }


# ---------------------------------------------------------------------------
# MCPTools bridge - FL Studio 2026 ships its own `MCPTools.pyc` (used by its
# built-in Gopher assistant) exposing ~47 tools this script's own ALLOWED
# table doesn't reach: add_effect/add_channel/remove_channels (create/destroy
# - Surface 1 in docs/01 cannot do this), plugin-parameter get/set by name,
# and run_piano_roll_script/save_piano_roll_script. Direct `import MCPTools`
# here is expected to fail with a bytecode-magic-number mismatch (MCPTools.pyc
# is compiled for CPython 3.12; this interpreter is CPython 3.9 per docs/01), reproduced
# empirically outside FL. mcp.import_tools/mcp.call below still attempt the
# live import at runtime - FL's actual embedded interpreter is unverified
# against a live run - and report success/failure rather than assume either.
# ---------------------------------------------------------------------------

_mcp_state = {"module": None}

# FL's default Tools/MCP dir, by platform. Pass tools_dir explicitly to
# mcp.import_tools if FL is installed elsewhere or under a different version
# folder - these are best-effort defaults, not the only candidates tried.
_DEFAULT_MCP_TOOLS_DIRS = [
    "/Applications/FL Studio 2026.app/Contents/Resources/FL/System/Tools/MCP",
    r"C:\Program Files\Image-Line\FL Studio 2026\System\Tools\MCP",
]

# The public MCPTools functions, hardcoded from decompiling MCPTools.pyc
# (stubbing its FL-module + `ilmcp` imports under a matching CPython 3.12 and
# reading `inspect.signature` per function). Not read live via get_mcp_tools_json(): that call reads a
# registry populated by the real `ilmcp.mcp_tool` decorator, which returns an
# empty list under any stand-in decorator, since nothing registered into it.
# Private (`_`-prefixed) helpers and the `mcp_tool`/`whenDone` names are
# deliberately excluded - only names FL's own assistant would call as a tool.
MCP_TOOL_WHITELIST = frozenset(
    {
        "add_channel", "add_effect", "get_browser_names", "get_channel_volumes",
        "get_highlight_names", "get_mcp_tools_json", "get_mixer_routing",
        "get_mixer_tracks_volume", "get_playlist_track_names",
        "get_plugin_parameter_list", "get_plugin_parameter_value",
        "get_session_context", "get_tempo", "list_channel_names",
        "mute_channels", "mute_mixer_tracks", "open_piano_roll_for_channel",
        "play", "quantize_channel", "remove_channels", "remove_effect",
        "rename_channels", "rename_mixer_tracks", "rename_playlist_tracks",
        "reset_mixer_routing", "reset_mixer_tracks", "route_channels_to_mixer",
        "run_piano_roll_script", "save_piano_roll_script", "select_channels",
        "set_channels_colour", "set_channels_pan", "set_channels_volume",
        "set_channels_volume_db", "set_mixer_routing", "set_mixer_tracks_colour",
        "set_mixer_tracks_pan", "set_mixer_tracks_volume_db",
        "set_mixer_tracks_volume_percent", "set_playlist_tracks_colour",
        "set_plugin_parameter_value", "set_step_sequencer_pattern",
        "set_step_sequencer_step", "set_tempo", "show_highlight",
        "show_or_hide_window", "solo_channels", "solo_mixer_tracks", "stop",
    }
)


class _IlmcpUnavailable:
    """Stand-in for FL's native `ilmcp` module when it isn't registered in
    this interpreter (live-verified: the MIDI-scripting context doesn't get
    it; only FL's own MCP/Gopher session does). Importing MCPTools with this
    stub keeps its ~40 public-API tools callable; the 8 ilmcp-bound ones
    raise a structured error instead of NameError at import time.
    """

    def __getattr__(self, name):
        def _unavailable(*_args, **_kwargs):
            raise RuntimeError(
                f"ilmcp.{name} is not reachable from the controller-script "
                "interpreter (native module registered only in FL's own MCP session)"
            )
        return _unavailable


def _import_mcp_tools(tools_dir=None):
    """Best-effort import of FL's bundled MCPTools module.

    Returns ``(imported: bool, detail: str)`` - on success `detail` is the
    directory that worked (with a ``[ilmcp stubbed]`` marker when the native
    module was absent and a stub was injected); on failure it's every
    candidate tried and why.
    """
    candidates = [tools_dir] if tools_dir else _DEFAULT_MCP_TOOLS_DIRS
    errors = []
    for candidate in candidates:
        if not candidate or not os.path.isdir(candidate):
            errors.append(f"{candidate!r}: not a directory")
            continue
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
        for stub_ilmcp in (False, True):
            if stub_ilmcp:
                if "ilmcp" in sys.modules:
                    break  # real ilmcp exists; the first attempt's error was something else
                sys.modules["ilmcp"] = _IlmcpUnavailable()
            try:
                _mcp_state["module"] = importlib.import_module("MCPTools")
                return True, candidate + (" [ilmcp stubbed]" if stub_ilmcp else "")
            except Exception as exc:
                if stub_ilmcp:
                    sys.modules.pop("ilmcp", None)
                sys.modules.pop("MCPTools", None)
                errors.append(
                    f"{candidate!r}{' [ilmcp stubbed]' if stub_ilmcp else ''}: "
                    f"{type(exc).__name__}: {exc}"
                )
    return False, "; ".join(errors)


def _mcp_import_tools(args, kwargs):
    tools_dir = kwargs.get("tools_dir") or (args[0] if args else None)
    ok, detail = _import_mcp_tools(tools_dir)
    if not ok:
        return {"imported": False, "err": detail, "whitelist": sorted(MCP_TOOL_WHITELIST)}
    module = _mcp_state["module"]
    present = sorted(name for name in MCP_TOOL_WHITELIST if callable(getattr(module, name, None)))
    missing = sorted(MCP_TOOL_WHITELIST - set(present))
    return {"imported": True, "tools_dir": detail, "available": present, "missing": missing}


def _mcp_call(args, kwargs):
    if not args:
        raise ValueError("mcp.call requires the MCPTools function name as args[0]")
    fn_name = args[0]
    if fn_name not in MCP_TOOL_WHITELIST:
        raise ValueError(f"{fn_name!r} is not a whitelisted MCPTools function")
    module = _mcp_state["module"]
    if module is None:
        raise RuntimeError("MCPTools not imported - call mcp.import_tools first")
    fn = getattr(module, fn_name, None)
    if not callable(fn):
        raise RuntimeError(f"MCPTools has no callable {fn_name!r}")
    return fn(*args[1:], **kwargs)


def _select_channels(args, kwargs):
    """Select exactly one channel for the Bridge's selected-channel write API."""
    if kwargs or len(args) != 1 or not isinstance(args[0], list) or len(args[0]) != 1:
        raise ValueError("select_channels requires exactly one channel index")
    channel = args[0][0]
    if not isinstance(channel, int) or isinstance(channel, bool) or channel < 0:
        raise ValueError("select_channels requires one non-negative integer channel index")
    channels.selectOneChannel(channel)
    return channel


COMPOSITE_OPS = {
    "state.snapshot": lambda args, kwargs: _state_snapshot(),
    "state.undo_probe": lambda args, kwargs: _undo_probe(),
    "ctl.save": lambda args, kwargs: transport.globalTransport(
        midi.FPT_Save,
        1,  # [ASSUMED value - live-verify]
        midi.PME_System,
        midi.GT_All,
    ),
    "select_channels": _select_channels,
    "mcp.import_tools": _mcp_import_tools,
    "mcp.call": _mcp_call,
    "ctl.hello": lambda args, kwargs: _hello_fields(),
}
COMPOSITE_NAMESPACES = ("state.", "mcp.", "ctl.")


# ---------------------------------------------------------------------------
# OnIdle deferral for FL's "unsafe at current time" guard (docs/15 fix item
# 1). FL sometimes refuses a mutating engine call (`mixer.setCurrentTempo`
# observed, docs/14) when it's dispatched from the OnSysEx callback
# mid-engine-cycle, but the same call succeeds moments later from OnIdle.
# Rather than fail the request, park it and retry on each OnIdle tick until
# it lands or a bounded number of ticks pass, then report the original error.
# This is scoped to the direct ALLOWED-module passthrough below (where every
# mutating op lives); composite/read ops are unaffected.
# ---------------------------------------------------------------------------

UNSAFE_AT_CURRENT_TIME = "unsafe at current time"
# ~50 OnIdle ticks (~1s at the ~20ms cadence docs/14 observed FL run OnIdle
# at) before giving up and replying with the error instead of waiting forever.
DEFER_MAX_TICKS = 200  # ~4s at FL's ~20ms OnIdle cadence; FL can stay unsafe for seconds right after a project load (measured live 2026-08-27)

_deferred_ops = []  # [{"message": dict, "req_id": int, "ticks": int}, ...]


def _resolve_fn(op):
    mod_name, _, fn_name = op.partition(".")
    mod = ALLOWED.get(mod_name)
    return getattr(mod, fn_name, None) if mod is not None and fn_name and not fn_name.startswith("_") else None


def _error_response(req_id, exc):
    return {
        "id": req_id,
        "ok": False,
        "err": f"{type(exc).__name__}: {exc}",
        "trace": traceback.format_exc(),
    }


def _retry_deferred_ops():
    """Called once per OnIdle tick. Retries every parked op; ops that land or
    give up are removed and get their (delayed) response sent now."""
    if not _deferred_ops:
        return
    still_pending = []
    for entry in _deferred_ops:
        message, req_id = entry["message"], entry["req_id"]
        fn = _resolve_fn(message.get("op", ""))
        try:
            ret = fn(*message.get("args", []), **message.get("kwargs", {}))
        except RuntimeError as exc:
            if UNSAFE_AT_CURRENT_TIME in str(exc):
                entry["ticks"] += 1
                if entry["ticks"] < DEFER_MAX_TICKS:
                    still_pending.append(entry)
                    continue
                _send_response(_error_response(req_id, exc))
                continue
            _send_response(_error_response(req_id, exc))
            continue
        except Exception as exc:
            _send_response(_error_response(req_id, exc))
            continue
        _send_response({"id": req_id, "ok": True, "ret": ret})
    _deferred_ops[:] = still_pending


def _dispatch(message):
    # Per-session addressing (#150): the shared MIDI wire broadcasts to every
    # FL instance; a request tagged for another FL's process id is not ours.
    # Returning None sends no response, so the addressed FL is the only voice.
    target_pid = message.get("pid")
    if target_pid is not None and target_pid != os.getpid():
        return None
    req_id = message.get("id")
    if not isinstance(req_id, int) or isinstance(req_id, bool):
        req_id = UNKNOWN_ID  # ControlResponse.id is a required int - never echo back None/non-int
    op = message.get("op", "")
    args = message.get("args", [])
    kwargs = message.get("kwargs", {})

    if op == "ping":
        return {"id": req_id, "ok": True, "ret": "pong"}

    composite = COMPOSITE_OPS.get(op)
    if composite is not None:
        try:
            return {"id": req_id, "ok": True, "ret": composite(args, kwargs)}
        except Exception as exc:
            return {"id": req_id, "ok": False, "err": f"{type(exc).__name__}: {exc}"}
    if op.startswith(COMPOSITE_NAMESPACES):
        return {"id": req_id, "ok": False, "err": f"unknown composite op {op!r}"}

    fn = _resolve_fn(op)
    if fn is None or not callable(fn):
        return {"id": req_id, "ok": False, "err": f"unknown op {op!r}"}
    try:
        ret = fn(*args, **kwargs)
    except RuntimeError as exc:
        if UNSAFE_AT_CURRENT_TIME in str(exc):
            _deferred_ops.append({"message": message, "req_id": req_id, "ticks": 0})
            return None  # no response yet - OnIdle retries and replies when it lands or gives up
        return _error_response(req_id, exc)
    except Exception as exc:
        return _error_response(req_id, exc)
    return {"id": req_id, "ok": True, "ret": ret}


# ---------------------------------------------------------------------------
# Wire I/O
# ---------------------------------------------------------------------------

_reassembler = Reassembler()
_event_seq = [0]


def _send_frames(frame_type, payload_obj, msg_id=None):
    if device is None:
        return  # no MIDI out assigned yet, or running outside FL (tests)
    for frame in encode_message(frame_type, payload_obj, msg_id=msg_id):
        device.midiOutSysex(frame)  # device.midiOutSysex(message: bytes) - VERIFIED


def _send_response(response_dict):
    _send_frames(TYPE_RESPONSE, response_dict, msg_id=response_dict.get("id"))


def _send_event(op, fields=None):
    payload = {"op": op}
    if fields:
        payload.update(fields)
    _event_seq[0] += 1
    _send_frames(TYPE_EVENT, payload, msg_id=_event_seq[0])


def _hello_fields():
    return {
        "script_ver": SCRIPT_VER,
        # ui.getVersion(mode=4) is the FL Studio version string. general.getVersion()
        # returns the *API* version (an int) - docs/05's draft table conflated the
        # two; corrected here.
        "fl_ver": ui.getVersion(),
        "ppq": general.getRecPPQ(),
        "tempo": mixer.getCurrentTempo(),
    }


# ---------------------------------------------------------------------------
# FL callbacks
# ---------------------------------------------------------------------------


def OnInit():
    _send_event("hello", _hello_fields())


def OnSysEx(event):
    try:
        decoded = decode_frame(event.sysex)
    except FrameError as exc:
        _send_response({"id": UNKNOWN_ID, "ok": False, "err": str(exc)})
        return
    if decoded is None:
        return  # not our traffic
    frame_type, payload = decoded
    if frame_type not in (TYPE_REQUEST, TYPE_CHUNK):
        return  # we only expect requests (and chunks of them) on our input port
    message = _reassembler.feed(frame_type, payload)
    if message is None:
        return  # chunk buffered, awaiting the rest of this message
    response = _dispatch(message)
    if response is None:
        return  # op deferred to OnIdle (unsafe-at-current-time guard); it replies when it lands or gives up
    _send_response(response)


def OnIdle():
    """Retry operations deferred by FL's unsafe-at-current-time guard."""
    _retry_deferred_ops()


def OnProjectLoad(status):
    """Re-announce hello on project switch so the server invalidates its live-
    state cache (docs/05). status values: midi.PL_Start/PL_LoadOk/PL_LoadError."""
    _send_event("project_load", {"status": status})
    if midi is not None and status == midi.PL_LoadOk:
        _send_event("hello", _hello_fields())


def OnDirtyChannel(index, *flags):
    """Freshness layer 1 (docs/11): structure changed, not note content.

    FL 2026 passes (index, updateType) - live-verified 2026-08-26 via a
    TypeError in the Script output window - while older docs show one arg,
    so accept both shapes.
    """
    _send_event("dirty_channel", {"index": index, "flags": list(flags)})


def OnDirtyMixerTrack(index, *flags):
    _send_event("dirty_mixer_track", {"index": index, "flags": list(flags)})


def OnRefresh(flags):
    _send_event("refresh", {"flags": flags})
