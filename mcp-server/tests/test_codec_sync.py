"""Keeps device_FLMCP.py's embedded wire codec in sync with flmcp_codec.py.

device_FLMCP.py must stay a standalone, stdlib-only script FL can load
directly, so it can't `import flmcp_codec` - it embeds the codec's source
verbatim instead (docs/15 fix item 6). This test is what makes drift between
the two copies impossible: if someone edits one and forgets the other, it
fails.
"""

from pathlib import Path

from fl_mcp.fl_scripts import flmcp_codec

_CONTROLLER_DIR = Path(flmcp_codec.__file__).parent
_CODEC_PATH = _CONTROLLER_DIR / "flmcp_codec.py"
_DEVICE_SCRIPT_PATH = _CONTROLLER_DIR / "device_FLMCP.py"

_BEGIN_MARKER = "# === BEGIN flmcp_codec.py (embedded verbatim) ===\n"
_END_MARKER = "# === END flmcp_codec.py ===\n"


def _embedded_codec_block():
    text = _DEVICE_SCRIPT_PATH.read_text()
    start = text.index(_BEGIN_MARKER) + len(_BEGIN_MARKER)
    end = text.index(_END_MARKER, start)
    return text[start:end]


def test_markers_present_exactly_once():
    text = _DEVICE_SCRIPT_PATH.read_text()
    assert text.count(_BEGIN_MARKER) == 1
    assert text.count(_END_MARKER) == 1


def test_embedded_codec_is_byte_identical_to_flmcp_codec_py():
    assert _embedded_codec_block() == _CODEC_PATH.read_text()


def test_flmcp_codec_is_independently_importable_and_round_trips():
    payload = {"id": 1, "op": "ping"}
    frame = flmcp_codec.encode_frame(flmcp_codec.TYPE_REQUEST, payload)
    assert flmcp_codec.decode_frame(frame) == (flmcp_codec.TYPE_REQUEST, payload)
