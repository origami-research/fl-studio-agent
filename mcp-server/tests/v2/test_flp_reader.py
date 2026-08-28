"""Lane F reader tests: synthetic FLP bytes, byte-precise, no FL required.

The builders below emit the exact framing docs/21 fact 3 describes, so every
assertion here is against the wire format itself rather than a parser fixture.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from fl_mcp.v2 import flp
from fl_mcp.v2.flp import FlpError


def event(event_id: int, payload: bytes) -> bytes:
    """Encode one event: fixed-size below 192, varint-length above."""
    if event_id < 192:
        expected = 1 if event_id < 64 else 2 if event_id < 128 else 4
        assert len(payload) == expected, "test bug: wrong fixed payload size"
        return bytes([event_id]) + payload
    head = bytearray([event_id])
    n = len(payload)
    while True:
        byte = n & 0x7F
        n >>= 7
        head.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes(head) + payload


def build_flp(events: bytes, ppq: int = 96, n_channels: int = 1) -> bytes:
    header = struct.pack("<HHH", 0, n_channels, ppq)
    return (
        b"FLhd" + struct.pack("<I", len(header)) + header
        + b"FLdt" + struct.pack("<I", len(events)) + events
    )


def note_record(
    position: int = 0,
    length: int = 24,
    key: int = 60,
    channel: int = 0,
    velocity: int = 100,
    pan: int = 64,
) -> bytes:
    return flp.NOTE_STRUCT.pack(
        position, 0, channel, length, key, 0,
        flp.FINE_PITCH_CENTER, 0, flp.RELEASE_DEFAULT, 0,
        pan, velocity, flp.MOD_DEFAULT, flp.MOD_DEFAULT,
    )


VERSION_MODERN = event(flp.EVENT_VERSION, b"20.8.3.0\0")
VERSION_ANCIENT = event(flp.EVENT_VERSION, b"10.0.9\0")


def write_flp(tmp_path: Path, events: bytes, **kwargs) -> Path:
    path = tmp_path / "test.flp"
    path.write_bytes(build_flp(events, **kwargs))
    return path


# -- framing ------------------------------------------------------------------


def test_non_flp_bytes_raise_with_offset(tmp_path):
    path = tmp_path / "bogus.flp"
    path.write_bytes(b"MThd not an flp at all")
    with pytest.raises(FlpError, match="FLhd"):
        flp.read(path)


def test_missing_fldt_raises_with_offset(tmp_path):
    path = tmp_path / "bad.flp"
    path.write_bytes(b"FLhd" + struct.pack("<I", 6) + b"\0" * 6 + b"XXXX")
    with pytest.raises(FlpError, match="offset 14"):
        flp.read(path)


def test_fldt_length_overrun_raises(tmp_path):
    path = tmp_path / "bad.flp"
    body = build_flp(b"")
    # Claim 100 bytes of events but provide none.
    path.write_bytes(body[:-4] + struct.pack("<I", 100))
    with pytest.raises(FlpError, match="overruns the file"):
        flp.read(path)


def test_truncated_event_payload_raises(tmp_path):
    # Event 156 promises 4 payload bytes but the stream ends after 2.
    path = write_flp(tmp_path, bytes([flp.EVENT_TEMPO]) + b"\x01\x02")
    with pytest.raises(FlpError, match="offset 0"):
        flp.read(path)


def test_truncated_varint_raises(tmp_path):
    # 0x80 continuation bit set, then the stream ends.
    path = write_flp(tmp_path, bytes([flp.EVENT_PATTERN_NOTES, 0x80]))
    with pytest.raises(FlpError, match="varint"):
        flp.read(path)


# -- header + tempo -----------------------------------------------------------


def test_ppq_and_tempo(tmp_path):
    path = write_flp(
        tmp_path,
        event(flp.EVENT_TEMPO, struct.pack("<I", 137_000)),
        ppq=192,
    )
    project = flp.read(path)
    assert project.ppq == 192
    assert project.tempo == 137.0


def test_tempo_is_none_when_event_omitted(tmp_path):
    project = flp.read(write_flp(tmp_path, VERSION_MODERN))
    assert project.tempo is None


# -- channels -----------------------------------------------------------------


def channel_block(iid: int, *extra: bytes) -> bytes:
    return event(flp.EVENT_CHANNEL_NEW, struct.pack("<H", iid)) + b"".join(extra)


def levels_payload(pan: int = 6400, volume: int = 10000, pitch: int = 0) -> bytes:
    return struct.pack("<iIi", pan, volume, pitch) + b"\0" * 12


def test_channel_name_utf16_modern(tmp_path):
    events = VERSION_MODERN + channel_block(
        0, event(flp.EVENT_NAME_USER, "Kick ♥\0".encode("utf-16-le"))
    )
    (channel,) = flp.read(write_flp(tmp_path, events)).channels
    assert channel.index == 0
    assert channel.name == "Kick ♥"


def test_channel_name_ascii_before_11_5(tmp_path):
    events = VERSION_ANCIENT + channel_block(
        3, event(flp.EVENT_NAME_LEGACY, b"OldKick\0")
    )
    (channel,) = flp.read(write_flp(tmp_path, events)).channels
    assert channel.index == 3
    assert channel.name == "OldKick"


def test_channel_name_priority_user_over_legacy_over_internal(tmp_path):
    def named(iid: int, *names: bytes) -> bytes:
        return channel_block(iid, *names)

    user = event(flp.EVENT_NAME_USER, "User\0".encode("utf-16-le"))
    legacy = event(flp.EVENT_NAME_LEGACY, "Legacy\0".encode("utf-16-le"))
    internal = event(flp.EVENT_NAME_INTERNAL, "Internal\0".encode("utf-16-le"))
    events = VERSION_MODERN + (
        named(0, internal, legacy, user)
        + named(1, internal, legacy)
        + named(2, internal)
        + named(3)
    )
    channels = flp.read(write_flp(tmp_path, events)).channels
    assert [c.name for c in channels] == ["User", "Legacy", "Internal", ""]


def test_mixer_section_names_do_not_leak_into_last_channel(tmp_path):
    events = VERSION_MODERN + channel_block(
        0, event(flp.EVENT_NAME_USER, "Kick\0".encode("utf-16-le"))
    )
    # Mixer section: InsertID.Flags, then a slot plugin name event.
    events += event(flp.EVENT_MIXER_FLAGS, b"\0" * 12)
    events += event(flp.EVENT_NAME_USER, "Reverb\0".encode("utf-16-le"))
    (channel,) = flp.read(write_flp(tmp_path, events)).channels
    assert channel.name == "Kick"


def test_undecodable_utf16_name_raises_flp_error(tmp_path):
    # Odd byte count cannot be UTF-16-LE; must surface as FlpError, not a crash.
    events = VERSION_MODERN + channel_block(0, event(flp.EVENT_NAME_USER, b"abc"))
    with pytest.raises(FlpError, match="utf-16-le"):
        flp.read(write_flp(tmp_path, events))


def test_channel_levels_event_219(tmp_path):
    events = VERSION_MODERN + channel_block(
        0, event(flp.EVENT_CHANNEL_LEVELS, levels_payload(pan=6372, volume=11000, pitch=-2))
    )
    (channel,) = flp.read(write_flp(tmp_path, events)).channels
    assert (channel.pan, channel.volume, channel.pitch_semitones) == (6372, 11000, -2)


def test_channel_levels_legacy_word_fallback(tmp_path):
    events = VERSION_MODERN + channel_block(
        0,
        event(flp.EVENT_VOL_WORD, struct.pack("<H", 9000)),
        event(flp.EVENT_PAN_WORD, struct.pack("<H", 5000)),
    )
    (channel,) = flp.read(write_flp(tmp_path, events)).channels
    assert (channel.volume, channel.pan, channel.pitch_semitones) == (9000, 5000, 0)


def test_channel_levels_event_wins_over_legacy_words(tmp_path):
    events = VERSION_MODERN + channel_block(
        0,
        event(flp.EVENT_VOL_WORD, struct.pack("<H", 1)),
        event(flp.EVENT_CHANNEL_LEVELS, levels_payload(volume=12000)),
    )
    (channel,) = flp.read(write_flp(tmp_path, events)).channels
    assert channel.volume == 12000


def test_channel_levels_default_when_absent(tmp_path):
    (channel,) = flp.read(write_flp(tmp_path, VERSION_MODERN + channel_block(0))).channels
    assert (channel.volume, channel.pan, channel.pitch_semitones) == (
        flp.VOLUME_DEFAULT, flp.PAN_CENTRE, 0,
    )


def test_malformed_levels_event_skipped_with_warning(tmp_path, caplog):
    events = VERSION_MODERN + channel_block(
        0, event(flp.EVENT_CHANNEL_LEVELS, b"\0" * 10)
    )
    with caplog.at_level("WARNING"):
        (channel,) = flp.read(write_flp(tmp_path, events)).channels
    assert channel.volume == flp.VOLUME_DEFAULT
    assert "Levels event of size 10" in caplog.text


# -- notes --------------------------------------------------------------------


def test_notes_across_patterns_and_channels(tmp_path):
    pattern_1 = event(flp.EVENT_PATTERN_NEW, struct.pack("<H", 1)) + event(
        flp.EVENT_PATTERN_NOTES,
        note_record(position=0, key=60, channel=0) + note_record(position=24, key=64, channel=1),
    )
    pattern_2 = event(flp.EVENT_PATTERN_NEW, struct.pack("<H", 2)) + event(
        flp.EVENT_PATTERN_NOTES, note_record(position=48, key=67, channel=0)
    )
    project = flp.read(write_flp(tmp_path, VERSION_MODERN + pattern_1 + pattern_2))

    assert len(project.notes) == 3
    assert [n.key for n in project.notes_in(1)] == [60, 64]
    assert [n.key for n in project.notes_in(1, channel=1)] == [64]
    (p2_note,) = project.notes_in(2)
    assert (p2_note.position, p2_note.key, p2_note.pattern) == (48, 67, 2)


def test_notes_at_reads_the_saved_file(tmp_path):
    events = VERSION_MODERN + event(
        flp.EVENT_PATTERN_NEW, struct.pack("<H", 4)
    ) + event(flp.EVENT_PATTERN_NOTES, note_record(key=72, channel=5, velocity=99, pan=10))
    path = write_flp(tmp_path, events)
    (note,) = flp.notes_at(path, 4, 5)
    assert (note.key, note.velocity, note.pan) == (72, 99, 10)
    assert flp.notes_at(path, 4, 6) == []
    assert flp.notes_at(path, 5, 5) == []


def test_nonconforming_notes_blob_skipped_with_warning(tmp_path, caplog):
    # A9: "Remix a song.flp" ships a size-34 Notes event before any pattern.
    events = VERSION_MODERN + event(flp.EVENT_PATTERN_NOTES, b"\x01" * 34)
    events += event(flp.EVENT_PATTERN_NEW, struct.pack("<H", 1))
    events += event(flp.EVENT_PATTERN_NOTES, note_record(key=61))
    with caplog.at_level("WARNING"):
        project = flp.read(write_flp(tmp_path, events))
    assert "size 34" in caplog.text
    assert [n.key for n in project.notes] == [61]


def test_valid_notes_blob_before_any_pattern_kept_unattributed(tmp_path):
    events = VERSION_MODERN + event(flp.EVENT_PATTERN_NOTES, note_record(key=50))
    project = flp.read(write_flp(tmp_path, events))
    (note,) = project.notes
    assert note.pattern == 0
    assert project.notes_in(1) == []


def test_decode_notes_rejects_truncated_blob():
    with pytest.raises(FlpError, match="25"):
        flp._decode_notes(b"\0" * 25)


def test_malformed_version_event_raises(tmp_path):
    path = write_flp(tmp_path, event(flp.EVENT_VERSION, b"not.a.version\0"))
    with pytest.raises(FlpError, match="FLVersion"):
        flp.read(path)
