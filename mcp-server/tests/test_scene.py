"""Tests for the scene summary.

The load-bearing property is not that key inference is clever - it is that it is
*honest*. A confidently wrong key would silently steer every subsequent note
choice, which is worse than reporting nothing. So most of these assert on the
confidence label, not just the key.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from fl_mcp import scene


@dataclass
class FakeRow:
    """Stands in for scene.SceneRow without needing a project file."""

    midi: int
    channel: int = 0
    start_beat: float = 0.0
    length_beats: float = 1.0
    velocity: int = 100


def weights(*midis: int) -> dict[int, int]:
    out: dict[int, int] = {}
    for m in midis:
        out[m % 12] = out.get(m % 12, 0) + 1
    return out


# --- key inference ----------------------------------------------------------


def test_unambiguous_c_major_is_found_with_confidence():
    # A full C major scale, tonic emphasised.
    key, confidence = scene._infer_key(weights(60, 60, 60, 62, 64, 65, 67, 69, 71))
    assert key == "C major"
    assert confidence in ("high", "medium")


def test_a_minor_distinguished_from_c_major_by_tonic_emphasis():
    """Same pitch classes; only the emphasised tonic differs.

    Without the tonic bonus these two are indistinguishable, which is the whole
    reason that term exists.
    """
    key, _ = scene._infer_key(weights(57, 57, 57, 57, 59, 60, 62, 64, 65, 67))
    assert key == "A minor"


def test_chromatic_material_reports_low_confidence():
    """All twelve pitch classes fit no key. Saying so is the correct answer."""
    _, confidence = scene._infer_key(weights(*range(60, 72)))
    assert confidence == "low"


def test_empty_input_is_unknown_not_a_guess():
    key, confidence = scene._infer_key({})
    assert key == "unknown"
    assert confidence == "none"


def test_two_notes_do_not_produce_high_confidence():
    """A fifth fits a dozen keys equally; claiming certainty would be a lie."""
    _, confidence = scene._infer_key(weights(60, 67))
    assert confidence != "high"


# --- root motion ------------------------------------------------------------


def test_root_motion_takes_the_lowest_note_per_bar():
    rows = [
        FakeRow(midi=50, start_beat=0),   # D, bar 0
        FakeRow(midi=62, start_beat=2),   # higher D, same bar - ignored
        FakeRow(midi=48, start_beat=4),   # C, bar 1
        FakeRow(midi=55, start_beat=8),   # G, bar 2
    ]
    assert scene._root_motion(rows) == ["D", "C", "G"]


def test_root_motion_is_capped_to_stay_compact():
    rows = [FakeRow(midi=48, start_beat=bar * 4) for bar in range(32)]
    motion = scene._root_motion(rows)
    assert len(motion) == scene.MAX_ROOT_MOTION_BARS + 1
    assert motion[-1] == "...+24 bars"


# --- summarize --------------------------------------------------------------


@pytest.fixture
def fake_notes(monkeypatch):
    """Patch the readers so these tests need no .flp on disk."""

    def install(rows, clips=(), automations=()):
        monkeypatch.setattr(scene, "_read_rows", lambda _path: rows)
        monkeypatch.setattr(scene, "_read_clips", lambda _path: list(clips))
        monkeypatch.setattr(scene, "_read_automations", lambda _path: list(automations))

    return install


def test_summary_groups_by_channel_and_names_them(fake_notes):
    fake_notes(
        [
            FakeRow(midi=60, channel=2, start_beat=0),
            FakeRow(midi=64, channel=2, start_beat=1),
            FakeRow(midi=40, channel=1, start_beat=0),
        ]
    )
    out = scene.summarize("ignored.flp", {1: "Bass", 2: "Lead"}, tempo=128)

    assert out["note_count"] == 3
    assert [c["name"] for c in out["channels"]] == ["Bass", "Lead"]
    assert "tempo 128" in out["summary"]
    assert "Lead (ch2): 2 notes" in out["summary"]


def test_bass_register_gets_root_motion_and_lead_does_not(fake_notes):
    fake_notes(
        [
            FakeRow(midi=40, channel=1, start_beat=0),   # E1 - bass register
            FakeRow(midi=72, channel=2, start_beat=0),   # C5 - well above it
        ]
    )
    out = scene.summarize("ignored.flp", {1: "Bass", 2: "Lead"})
    by_name = {c["name"]: c for c in out["channels"]}

    assert "root_motion" in by_name["Bass"]
    assert "root_motion" not in by_name["Lead"]


def test_empty_project_says_so_rather_than_failing(fake_notes):
    fake_notes([])
    out = scene.summarize("ignored.flp", {}, tempo=120)
    assert out["note_count"] == 0
    assert "no notes" in out["summary"]


def test_summary_is_far_cheaper_than_the_raw_notes(fake_notes):
    """The entire point of this module, asserted rather than assumed."""
    rows = [
        FakeRow(midi=60 + (i % 12), channel=0, start_beat=i * 0.25)
        for i in range(256)
    ]
    fake_notes(rows)
    out = scene.summarize("ignored.flp", {0: "Lead"}, tempo=130)

    raw_chars = sum(len(f'{{"midi":{r.midi},"channel":{r.channel},'
                        f'"start_beat":{r.start_beat},"length_beats":{r.length_beats}}}')
                    for r in rows)
    assert len(out["summary"]) * 10 < raw_chars, (
        f"summary {len(out['summary'])} chars vs raw {raw_chars} - "
        "expected at least a 10x reduction"
    )


def test_note_names_round_trip_the_octave_convention():
    # C4 == MIDI 60 in this project's scientific-pitch convention.
    assert scene.note_name(60) == "C4"
    assert scene.note_name(57) == "A3"
    assert scene.note_name(72) == "C5"


# --- playlist ----------------------------------------------------------------


def test_summary_lists_playlist_clips_when_few(fake_notes):
    fake_notes(
        [FakeRow(midi=60, channel=0)],
        clips=[
            scene.ClipRow(track=2, start_beat=16.0, length_beats=16.0, pattern=2),
            scene.ClipRow(track=1, start_beat=0.0, length_beats=16.0, pattern=1),
            scene.ClipRow(track=3, start_beat=0.0, length_beats=8.0, channel=5),
        ],
    )
    out = scene.summarize("ignored.flp", {}, tempo=120)
    assert out["playlist"]["clip_count"] == 3
    assert out["playlist"]["tracks_used"] == 3
    assert out["playlist"]["span_beats"] == [0.0, 32.0]
    assert out["playlist"]["clips"] == [
        {"pattern": 1, "track": 1, "at": 0.0, "beats": 16.0},
        {"channel": 5, "track": 3, "at": 0.0, "beats": 8.0},
        {"pattern": 2, "track": 2, "at": 16.0, "beats": 16.0},
    ]
    assert "playlist: 3 clips on 3 tracks, beats 0-32" in out["summary"]


def test_summary_digests_playlist_when_many(fake_notes):
    many = [
        scene.ClipRow(track=1 + i % 7, start_beat=float(i * 4), length_beats=4.0, pattern=1)
        for i in range(scene.MAX_PLAYLIST_CLIPS_LISTED + 1)
    ]
    fake_notes([FakeRow(midi=60, channel=0)], clips=many)
    out = scene.summarize("ignored.flp", {})
    assert out["playlist"]["clip_count"] == len(many)
    assert "clips" not in out["playlist"]


def test_summary_reports_empty_playlist_honestly(fake_notes):
    fake_notes([FakeRow(midi=60, channel=0)], clips=[])
    out = scene.summarize("ignored.flp", {})
    assert out["playlist"] == {"clip_count": 0}
    assert "playlist: empty" in out["summary"]


def test_empty_project_still_carries_the_playlist(fake_notes):
    fake_notes([], clips=[scene.ClipRow(track=1, start_beat=0.0, length_beats=4.0, pattern=1)])
    out = scene.summarize("ignored.flp", {})
    assert out["playlist"]["clip_count"] == 1


# --- automation ----------------------------------------------------------------


def test_summary_reports_automation_channels(fake_notes):
    fake_notes(
        [FakeRow(midi=60, channel=0)],
        automations=[
            scene.AutomationRow(
                channel=5, name="Volume - Master", points=3,
                span_beats=16.0, from_value=1.0, to_value=0.0,
            )
        ],
    )
    out = scene.summarize("ignored.flp", {})
    assert out["automations"] == [
        {
            "channel": 5,
            "name": "Volume - Master",
            "points": 3,
            "span_beats": 16.0,
            "values": [1.0, 0.0],
        }
    ]
    assert "automation: Volume - Master (3 pts over 16 beats, 1.00 -> 0.00)" in out["summary"]


def test_summary_omits_automation_key_when_none(fake_notes):
    fake_notes([FakeRow(midi=60, channel=0)])
    assert "automations" not in scene.summarize("ignored.flp", {})
