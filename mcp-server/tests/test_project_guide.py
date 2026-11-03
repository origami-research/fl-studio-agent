"""The project's own notes say what each channel is for (docs/07 "The session template").

The demo session's models put chords on a bass synth because nothing told them which channel
was which (2026-10-28); the packaged template now writes its palette into FL's Project info,
and get_scene and fl.channels read it from there, for any project that does the same.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from flpkit import codec
from session_fakes import FIVE_CHANNEL_PROJECT

from fl_mcp import flp, scene
from fl_mcp.flp import EVENT_COMMENTS

NAMES = ["808 Kick", "808 Clap", "808 HiHat", "808 Snare", "FLEX Bass"]


def with_comments(tmp_path: Path, text: str) -> Path:
    """The five-channel fixture with ``text`` as its Project info comments."""
    path = tmp_path / "project.flp"
    shutil.copyfile(FIVE_CHANNEL_PROJECT, path)
    data = bytearray(path.read_bytes())
    header, raw = codec.chunks(bytes(data))
    base = 8 + len(header) + 8
    stream = codec.Stream(raw)
    head = 0
    for event_id, offset, size in stream:
        if event_id == EVENT_COMMENTS:
            event = codec.frame_event(EVENT_COMMENTS, (text + "\0").encode("utf-16-le"))
            data[base + head : base + offset + size] = event
            data[base - 4 : base] = (len(data) - base).to_bytes(4, "little")
            path.write_bytes(bytes(data))
            return path
        head = offset + size
    raise AssertionError("the fixture has no comments event")


def test_a_line_naming_a_channel_says_what_it_is_for(tmp_path):
    project = with_comments(
        tmp_path,
        "Song lives in Pattern 1.\r808 Kick: the kick, notes 36.\rFLEX Bass: a synth bass, MIDI 28-55.\r\rMixer: one track each.",
    )
    guide = flp.project_guide(project, NAMES)

    assert guide.about == {"808 Kick": "the kick, notes 36.", "FLEX Bass": "a synth bass, MIDI 28-55."}
    assert guide.notes == "Song lives in Pattern 1.\nMixer: one track each."


def test_a_name_that_is_not_a_channel_stays_a_project_note(tmp_path):
    project = with_comments(tmp_path, "Strings: not in this project.\n808 Snare: the snare.")

    guide = flp.project_guide(project, NAMES)

    assert guide.about == {"808 Snare": "the snare."}
    assert guide.notes == "Strings: not in this project."


def test_a_project_without_notes_has_an_empty_guide(tmp_path):
    project = with_comments(tmp_path, "")

    assert flp.project_guide(project, NAMES) == flp.ProjectGuide()


def test_get_scene_summary_carries_each_channels_about_and_the_notes():
    out = scene.summarize(
        FIVE_CHANNEL_PROJECT, [], {0: "808 Kick", 4: "FLEX Bass"}, tempo=120,
        about={4: "a synth bass"}, project_notes="Song lives in Pattern 1.",
    )

    assert out["project_notes"] == "Song lives in Pattern 1."
    assert out["channels"] == [
        {"index": 0, "name": "808 Kick", "notes": 0},
        {"index": 4, "name": "FLEX Bass", "notes": 0, "about": "a synth bass"},
    ]
    assert out["summary"].splitlines()[0] == "project notes: Song lives in Pattern 1."
