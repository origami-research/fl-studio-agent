"""Lane F writer tests: raw byte surgery, verified by re-reading the file.

set_tempo is a PORT of the live-verified v1 (flp_writer.set_tempo, commit
13f8afc): the byte-differential test below asserts both produce identical
files, so any byte-level drift in the port fails loudly.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

from fl_mcp import flp_writer
from fl_mcp.flpaths import fl_app
from fl_mcp.v2 import flp

from .test_flp_reader import VERSION_MODERN, build_flp, event, write_flp

_FL_DATA = fl_app() / "Contents/Resources/FL/Data"

requires_fl = pytest.mark.skipif(
    not _FL_DATA.is_dir(), reason="FL Studio is not installed on this machine"
)


# -- set_tempo ----------------------------------------------------------------


def test_set_tempo_patches_existing_event_in_place(tmp_path):
    events = (
        VERSION_MODERN
        + event(flp.EVENT_TEMPO, struct.pack("<I", 140_000))
        + event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", 0))
    )
    path = write_flp(tmp_path, events)
    before = path.read_bytes()

    assert flp.set_tempo(path, 137) == 137.0

    after = path.read_bytes()
    assert len(after) == len(before)  # in-place patch, no splice
    assert flp.read(path).tempo == 137.0


def test_set_tempo_appends_at_end_of_stream_when_absent(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    before = path.read_bytes()

    assert flp.set_tempo(path, 133) == 133.0

    after = path.read_bytes()
    # The event lands literally at the end of the stream, and the FLdt length
    # grows by exactly the 5 appended bytes - the live-verified v1 decision.
    assert after[-5:] == bytes([flp.EVENT_TEMPO]) + struct.pack("<I", 133_000)
    assert len(after) == len(before) + 5
    assert flp.read(path).tempo == 133.0


def test_set_tempo_rejects_out_of_range():
    for bpm in (9.9, 999.1, 0, -10):
        with pytest.raises(ValueError, match="out of range"):
            flp.set_tempo(Path("/nonexistent.flp"), bpm)


def test_set_tempo_readback_is_the_stored_quantized_value(tmp_path):
    path = write_flp(tmp_path, VERSION_MODERN)
    stored = flp.set_tempo(path, 128.5004)
    assert stored == round(128.5004 * 1000) / 1000


def test_set_tempo_byte_identical_to_v1_port_source(tmp_path):
    """Both implementations must write the same bytes - the port contract."""
    events = VERSION_MODERN + event(flp.EVENT_TEMPO, struct.pack("<I", 140_000))
    for name, payload in (("with_tempo", events), ("no_tempo", VERSION_MODERN)):
        v1_path = tmp_path / f"{name}_v1.flp"
        v2_path = tmp_path / f"{name}_v2.flp"
        v1_path.write_bytes(build_flp(payload))
        v2_path.write_bytes(build_flp(payload))

        flp_writer.set_tempo(v1_path, 117)
        flp.set_tempo(v2_path, 117)

        assert v1_path.read_bytes() == v2_path.read_bytes()


@requires_fl
def test_set_tempo_on_stock_templates_both_paths(tmp_path):
    # Empty.flp carries a tempo event (patch path); Basic with limiter omits
    # it (append path) - the same pair the live e2e uses.
    cases = [
        ("Templates/Minimal/Empty/Empty.flp", "patch"),
        ("Templates/Minimal/Basic with limiter/Basic with limiter.flp", "append"),
    ]
    for rel, label in cases:
        path = tmp_path / f"{label}.flp"
        shutil.copy(_FL_DATA / rel, path)
        grew = len(path.read_bytes())

        assert flp.set_tempo(path, 123) == 123.0

        expected_growth = 0 if label == "patch" else 5
        assert len(path.read_bytes()) == grew + expected_growth
