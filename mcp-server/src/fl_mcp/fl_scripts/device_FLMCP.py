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

import contextlib
import os
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

SCRIPT_VER = "0.2.8"

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


# Channels are always FL's global index (the trailing 1 / GLOBAL below): without it FL
# counts only the channel rack's active filter group, so an index means a different
# channel than in the song file. The server's direct calls get the same flag
# (session/link.py GLOBAL_INDEX_FLAG_AT).
GLOBAL = 1


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
                "name": channels.getChannelName(i, GLOBAL),
                "muted": bool(channels.isChannelMuted(i, GLOBAL)),
                "vol": channels.getChannelVolume(i, False, GLOBAL),
                "pan": channels.getChannelPan(i, GLOBAL),
                "target_mixer_track": channels.getTargetFxTrack(i, GLOBAL),
            }
            for i in range(channels.channelCount(GLOBAL))
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


MIXER_SLOTS = 10


def _mixer_tracks():
    """`state.mixer` - the mixer tracks in use, with their effects and sends, in one
    round trip: fl.mixer. A track is in use when it is the master, a channel plays
    into it, it holds an effect, it was renamed from FL's "Insert N", or a track in
    use sends to it. Sends are scanned only from tracks in use (each scan is one
    getRouteSendActive per track), so a 127-track mixer costs a few thousand calls
    inside FL rather than sixteen thousand."""
    count = mixer.trackCount()
    rows = {}

    def row(i):
        if i not in rows:
            rows[i] = {
                "idx": i,
                "name": mixer.getTrackName(i),
                "vol": mixer.getTrackVolume(i),
                "pan": mixer.getTrackPan(i),
                "muted": bool(mixer.isTrackMuted(i)),
                "solo": bool(mixer.isTrackSolo(i)),
                "effects": [
                    [slot, plugins.getPluginName(i, slot)]
                    for slot in range(MIXER_SLOTS)
                    if mixer.isTrackPluginValid(i, slot)
                ],
                "sends": [],
            }
        return rows[i]

    used = {0}
    used.update(channels.getTargetFxTrack(c, GLOBAL) for c in range(channels.channelCount(GLOBAL)))
    for i in range(1, count):
        if mixer.getTrackName(i) != "Insert %d" % i or any(mixer.isTrackPluginValid(i, s) for s in range(MIXER_SLOTS)):
            used.add(i)
    pending = sorted(t for t in used if 0 <= t < count)
    scanned = set()
    while pending:
        i = pending.pop()
        if i in scanned:
            continue
        scanned.add(i)
        sends = row(i)["sends"]
        for target in range(count):
            if target != i and mixer.getRouteSendActive(i, target):
                sends.append([target, mixer.getRouteToLevel(i, target)])
                if target not in scanned:
                    pending.append(target)
    return [rows[i] for i in sorted(rows)]


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


def _select_channels(args, kwargs):
    """Select exactly one channel for the Bridge's selected-channel write API."""
    if kwargs or len(args) != 1 or not isinstance(args[0], list) or len(args[0]) != 1:
        raise ValueError("select_channels requires exactly one channel index")
    channel = args[0][0]
    if not isinstance(channel, int) or isinstance(channel, bool) or channel < 0:
        raise ValueError("select_channels requires one non-negative integer channel index")
    channels.selectOneChannel(channel, GLOBAL)
    return channel


def _set_mutes(args, kwargs):
    """`ctl.set_mutes` - set many channels' mutes in one round trip, each explicitly (1 mutes,
    0 unmutes; never a toggle, so a repeat is harmless), and read every one back:
    [[index, 0 or 1], ...] -> [[index, muted], ...]. A solo render on a 23-channel song
    sent each change as its own pair of requests around playback, and FL stopped
    answering midway (issue #309)."""
    if kwargs or len(args) != 1 or not isinstance(args[0], list):
        raise ValueError("set_mutes requires one list of [channel index, 0 or 1] pairs")
    for index, value in args[0]:
        channels.muteChannel(index, value, GLOBAL)
    return [[index, bool(channels.isChannelMuted(index, GLOBAL))] for index, _ in args[0]]


# getTrackPeaks mode: the louder of left and right (IL API stubs, midi.PEAK_LR).
PEAK_LR = 2


def _peaks(args, kwargs):
    """`state.peaks` - every requested mixer track's current meter peak (1.0 = 0 dBFS) in
    one round trip: [track, ...] -> [[track, peak], ...]. fl.meters reads this once per
    sweep while FL plays; one request per track (24 every 50 ms on the 23-channel default
    project) left FL not answering mid-playback (issue #317)."""
    if kwargs or len(args) != 1 or not isinstance(args[0], list):
        raise ValueError("peaks requires one list of mixer track indices")
    return [[track, mixer.getTrackPeaks(track, PEAK_LR)] for track in args[0]]


# ---------------------------------------------------------------------------
# Recording jobs - the documented way FL writes automation and puts a pattern in the
# playlist live (docs/research/arrangement-automation-live-2026-11-09.md). FL records a
# control's moves into the selected pattern while it records, and in song mode it adds
# that pattern's clip where playback started (FL manual, "Recording automation" and
# "Note / MIDI recording"). A job runs on OnIdle, one step per tick, because the record
# question FL may ask is a modal that froze FL when raised inside OnSysEx (live).
# ---------------------------------------------------------------------------

# Name FL gives the Bridge effect; its slot's mix level is the inert control a placement
# records (the Bridge passes audio through unchanged, so its mix changes nothing heard).
BRIDGE_PLUGIN_NAME = "FL MCP Bridge"
# FL's transport state (recording armed, playing) shows a few ticks after the call.
JOB_STEP_SECONDS = 3.0
# A job that records past its window by this much is stopped and reported.
JOB_SLACK_SECONDS = 5.0
# A failed job keeps FL stopped and not recording this long before reporting.
JOB_CLEANUP_SECONDS = 1.0
# A play job reads which playlist tracks are active this many ticks after the start.
ACTIVITY_SETTLE_TICKS = 3
# A playback keeps at most this many samples, evenly spaced, and reports them only once
# it ends: a status carrying every tick's sample (about 400 for 16 beats), asked for
# five times a second while FL played, was followed by FL no longer answering (take,
# 2026-11-09), the kind of flood #317 removed from the meters.
SAMPLE_ROWS_MAX = 64

_jobs = {"current": None, "last": None, "seq": 0}


def _now():
    import time

    return time.time()


def _find_bridge():
    for track in range(mixer.trackCount()):
        for slot in range(MIXER_SLOTS):
            if mixer.isTrackPluginValid(track, slot) and plugins.getPluginName(track, slot) == BRIDGE_PLUGIN_NAME:
                return track, slot
    raise ValueError("no mixer track holds the FL MCP plugin")


def _target_event(target):
    """FL's REC event id for a control: a channel's volume, pan or plugin parameter, a
    mixer track's volume or pan, an effect's parameter or mix level, or the Bridge's mix."""
    kind, param = target.get("kind"), target.get("param")
    if kind == "channel":
        base = channels.getRecEventId(target["channel"], GLOBAL)
        named = {"volume": midi.REC_Chan_Vol, "pan": midi.REC_Chan_Pan}
        if param in named:
            return base + named[param]
        if isinstance(param, int) and param >= 0:
            return base + midi.REC_Chan_Plugin_First + param
    elif kind == "mixer":
        base = mixer.getTrackPluginId(target["track"], 0)
        named = {"volume": midi.REC_Mixer_Vol, "pan": midi.REC_Mixer_Pan}
        if param in named:
            return base + named[param]
    elif kind == "effect":
        base = mixer.getTrackPluginId(target["track"], target["slot"])
        if param == "mix":
            return base + midi.REC_Plug_MixLevel
        if isinstance(param, int) and param >= 0:
            return base + midi.REC_Plug_Plugin_First + param
    elif kind == "bridge":
        track, slot = _find_bridge()
        return mixer.getTrackPluginId(track, slot) + midi.REC_Plug_MixLevel
    raise ValueError("cannot automate %r" % (target,))


def _value_at(points, tick):
    """The straight-line value of ``points`` ([[tick, 0..1], ...], sorted) at ``tick``."""
    if tick <= points[0][0]:
        return points[0][1]
    for i in range(1, len(points)):  # FL's Python 3.9 has no itertools.pairwise
        (t0, v0), (t1, v1) = points[i - 1], points[i]
        if tick <= t1:
            return v0 + (v1 - v0) * (tick - t0) / max(1, t1 - t0)
    return points[-1][1]


def _start_job(args, kwargs):
    """`ctl.record` - start one job, a recording or a playback; `ctl.job` reports it.

    spec: {"mode": "record" | "play", "start": abs tick, "length": ticks, "seconds":
    expected length, "pattern": record into (record), "target": control (record: what is
    recorded; play: what is sampled, optional), "points": [[tick from start, 0..1], ...]
    (record; None holds the control's current value), "tracks": play reads which playlist
    tracks are active at the start, "marker": the name of the time marker FL needs to
    start past the song's end}."""
    if kwargs or len(args) != 1 or not isinstance(args[0], dict):
        raise ValueError("a job takes one spec object")
    if _jobs["current"] is not None:
        raise RuntimeError("a recording job is already running")
    spec = dict(args[0])
    if spec.get("mode") not in ("record", "play"):
        raise ValueError("mode must be record or play")
    event = _target_event(spec["target"]) if spec.get("target") else None
    if transport.isRecording():  # toggling record would end the person's own recording
        raise RuntimeError("FL is already recording")
    if spec["mode"] == "record":
        if event is None:
            raise ValueError("a recording needs a target")
        points = spec.get("points") or [[0, device.getLinkedValue(event)]]
    else:
        points = None
    _jobs["seq"] += 1
    _jobs["current"] = {
        "id": _jobs["seq"], "spec": spec, "event": event, "points": points, "phase": "prepare",
        "since": _now(), "events": 0, "first": None, "last": None, "gaps": [0] * SAMPLE_ROWS_MAX, "rows": [],
        "tracks": None, "ticks": 0,
    }
    return {"job": _jobs["seq"]}


def _job_status(args, kwargs):
    job = _jobs["current"] or _jobs["last"]
    if job is None:
        return None
    keys = ("id", "phase", "events", "first", "last", "tracks", "error")
    status = {key: job.get(key) for key in keys}
    if job["phase"] in ("done", "failed"):
        status["rows"] = job["rows"]
        status["gaps"] = job["gaps"]
    return status


def _finish(job, error=None):
    job["phase"] = "failed" if error is not None else "done"
    if error is not None:
        job["error"] = error
    _jobs["last"], _jobs["current"] = job, None


def _fail(job, error):
    """Stop the job and leave FL stopped and not recording before reporting it failed:
    an arm FL shows a tick or more later is undone too (JOB_CLEANUP_SECONDS)."""
    job["error"], job["phase"], job["since"], job["disarm_sent"] = error, "cleanup", _now(), False


def _cleanup_step(job):
    for step in (_cleanup_stop, _cleanup_disarm):
        with contextlib.suppress(Exception):  # one failing call must not skip the other
            step(job)
    settled = not job.get("playing_seen") and not job.get("recording_seen")
    waited = _now() - job["since"]
    if (settled and waited >= JOB_CLEANUP_SECONDS) or waited >= 3 * JOB_CLEANUP_SECONDS:
        _finish(job, job["error"])


def _cleanup_stop(job):
    job["playing_seen"] = bool(transport.isPlaying())
    if job["playing_seen"]:
        transport.stop()


def _cleanup_disarm(job):
    job["recording_seen"] = bool(transport.isRecording())
    if job["recording_seen"] and not job["disarm_sent"]:
        transport.record()
        job["disarm_sent"] = True
    elif not job["recording_seen"]:
        job["disarm_sent"] = False


def _cancel_job(args, kwargs):
    """`ctl.cancel` - the engine stopped waiting: stop the job, leave FL stopped, not recording."""
    job = _jobs["current"]
    if job is None or job["phase"] == "cleanup":
        return None
    _fail(job, "the engine stopped waiting for it")
    return job["id"]


def _job_tick():
    job = _jobs["current"]
    if job is None:
        return
    if job["phase"] == "cleanup":
        _cleanup_step(job)
        return
    try:
        _job_step(job)
    except Exception as exc:
        _fail(job, "%s: %s" % (type(exc).__name__, exc))


def _job_step(job):
    spec, phase = job["spec"], job["phase"]
    start, end = spec["start"], spec["start"] + spec["length"]
    waited = _now() - job["since"]

    def advance(next_phase):
        job["phase"], job["since"] = next_phase, _now()

    if phase == "prepare":
        if transport.isPlaying():  # start() would pause it, not play from the start
            transport.stop()
        if spec["mode"] == "record":
            patterns.jumpToPattern(spec["pattern"])
        if transport.getLoopMode() != 1:
            transport.setLoopMode()
        advance("song")
    elif phase == "song":
        # The song position means the song's only once FL is in song mode, and FL moves
        # it no further than the song's end; a time marker extends the song (live).
        if transport.getLoopMode() == 1 and not transport.isPlaying():
            if start > transport.getSongLength(midi.SONGLENGTH_ABSTICKS) + 1:
                if spec["mode"] == "play":  # nothing plays past the song's end
                    job["tracks"] = []
                    _finish(job)
                    return
                arrangement.addAutoTimeMarker(start, spec.get("marker") or "")
            transport.setSongPos(start, midi.SONGLENGTH_ABSTICKS)
            if spec["mode"] == "record":
                transport.record()
                advance("arming")
            else:
                advance("placed")
        elif waited > JOB_STEP_SECONDS:
            _fail(job, "FL did not stop and switch to song mode")
    elif phase == "arming":
        if transport.isRecording():
            advance("placed")
        elif waited > JOB_STEP_SECONDS:
            _fail(job, "FL did not arm recording")
    elif phase == "placed":
        if abs(arrangement.currentTime(0) - start) <= 1:
            transport.start()
            advance("starting")
        elif waited > JOB_STEP_SECONDS:
            _fail(job, "FL did not move to the start position")
        else:  # the song's new end (a marker) may show a tick later
            transport.setSongPos(start, midi.SONGLENGTH_ABSTICKS)
    elif phase == "starting":
        if transport.isPlaying():
            advance("running")
        elif waited > JOB_STEP_SECONDS:
            _fail(job, "FL did not start playing")
    elif phase == "running":
        # getSongPos stops at the song's end; currentTime is where FL is playing.
        pos = arrangement.currentTime(0)
        # FL went back to the song's start: from bar 1 that is not before the start, so
        # any step backwards counts (a take from bar 1 to the song's end played on).
        looped = job["last"] is not None and pos < job["last"]
        if start <= pos < end and not looped:
            job["first"] = pos if job["first"] is None else job["first"]
            # The widest step between ticks in each of SAMPLE_ROWS_MAX spans of the take: a
            # recorded value holds that long on playback, and one slow tick widens only its span.
            if job["last"] is not None:
                span = min(SAMPLE_ROWS_MAX - 1, int((pos - start) * SAMPLE_ROWS_MAX // max(1, spec["length"])))
                job["gaps"][span] = max(job["gaps"][span], pos - job["last"])
            job["last"] = pos
            job["ticks"] += 1
            if spec["mode"] == "record":
                value = _value_at(job["points"], pos - start)
                general.processRECEvent(job["event"], round(value * midi.FromMIDI_Max), midi.REC_MIDIController)
                job["events"] += 1
            else:
                spacing = spec["length"] / SAMPLE_ROWS_MAX
                if job["event"] is not None and (not job["rows"] or pos - job["rows"][-1][0] >= spacing):
                    job["rows"].append([pos, device.getLinkedValue(job["event"])])
                if spec.get("tracks") and job["tracks"] is None and job["ticks"] >= ACTIVITY_SETTLE_TICKS:
                    job["tracks"] = [
                        t for t in range(1, playlist.trackCount() + 1) if playlist.getTrackActivityLevel(t) > 0
                    ]
        if spec.get("tracks") and job["tracks"] is None and pos >= end:
            job["tracks"] = [t for t in range(1, playlist.trackCount() + 1) if playlist.getTrackActivityLevel(t) > 0]
        if pos >= end or looped or not transport.isPlaying():
            transport.stop()
            advance("stopping")
        elif waited > spec.get("seconds", 0) + JOB_SLACK_SECONDS:
            _fail(job, "FL played past the job's window")
    elif phase == "stopping":
        if not transport.isPlaying():
            if spec["mode"] == "record":
                transport.record()
                advance("disarming")
            else:
                transport.setSongPos(start, midi.SONGLENGTH_ABSTICKS)
                _finish(job)
        elif waited > JOB_STEP_SECONDS:
            _fail(job, "FL did not stop")
    elif phase == "disarming":
        if not transport.isRecording():
            transport.setSongPos(start, midi.SONGLENGTH_ABSTICKS)
            _finish(job)
        elif waited > JOB_STEP_SECONDS:
            _fail(job, "FL did not stop recording")


COMPOSITE_OPS = {
    "state.snapshot": lambda args, kwargs: _state_snapshot(),
    "state.undo_probe": lambda args, kwargs: _undo_probe(),
    "state.mixer": lambda args, kwargs: _mixer_tracks(),
    "state.peaks": _peaks,
    "ctl.save": lambda args, kwargs: transport.globalTransport(
        midi.FPT_Save,
        1,  # [ASSUMED value - live-verify]
        midi.PME_System,
        midi.GT_All,
    ),
    "select_channels": _select_channels,
    "ctl.set_mutes": _set_mutes,
    "ctl.record": _start_job,
    "ctl.job": _job_status,
    "ctl.cancel": _cancel_job,
    "ctl.hello": lambda args, kwargs: _hello_fields(),
}
COMPOSITE_NAMESPACES = ("state.", "ctl.")


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
    """Retry operations deferred by FL's unsafe-at-current-time guard; step a recording job."""
    _retry_deferred_ops()
    _job_tick()


def OnProjectLoad(status):
    """Re-announce hello on project switch so the server invalidates its live-
    state cache (docs/05). status values: midi.PL_Start/PL_LoadOk/PL_LoadError."""
    _send_event("project_load", {"status": status})
    if midi is not None and status == midi.PL_LoadOk:
        _send_event("hello", _hello_fields())


# No OnRefresh, OnDirtyChannel or OnDirtyMixerTrack: FL calls them for every change it
# shows, which while automation plays a mixer track's volume is every few milliseconds,
# and a SysEx event for each (which the engine never read) went out while FL played.
# After the first 16-bar take of a mixer-volume automation FL stopped answering for good
# (fl-mcp-03, 2026-11-10; docs/research/live-pumping-2026-11-10.md).
