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
# frames is unverified and out of scope here [ASSUMED cap holds; NOTES.md].
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
