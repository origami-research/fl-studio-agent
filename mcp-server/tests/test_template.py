from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from sysconfig import get_path

import flpkit
from flpkit import codec
from flpkit.codec import Stream
from flpkit.formats import ShowInfoFormat
from flpkit.formats.playlist import EVENT_PLAYLIST, PlaylistFormat

from fl_mcp import flp, make_agent_project
from fl_mcp.template import template_path

TEMPLATE_SHA256 = "ff981e71a355e68f3b88a126d63107633e41fa3183ab5778951baa60c92d8751"

# Record bytes 24-31 (the cut window) as FL itself saves a pattern clip that was never
# resized: FL sizes such a clip from its pattern and regrows it whenever the pattern grows.
# An explicit window (0..length) pins the clip, so Full-song renders stop at it.
AUTO_LENGTH_WINDOW = b"\xff" * 8


def _template_command() -> str:
    scripts_dir = get_path("scripts")
    assert scripts_dir is not None
    return str(Path(scripts_dir) / "fl-mcp-template")


def test_packaged_template_has_pattern_one_on_the_playlist():
    project = flp.read(template_path())

    assert project.ppq == 48  # the stock template it is built from (scripts/build_session_template.py)
    assert project.playlist == (
        flp.FlpPlaylistItem(
            position=0, length=16 * 48, track=1, pattern=1, channel=None, arrangement=0
        ),
    )
    assert project.notes == ()


def test_template_clip_follows_pattern_one_length():
    data = template_path().read_bytes()
    (payload,) = [data[off : off + size] for event, off, size in Stream(data) if event == EVENT_PLAYLIST]
    (clip,) = PlaylistFormat().decode(payload, ppq=48)

    assert clip.pattern == 1
    assert clip.tail[8:16] == AUTO_LENGTH_WINDOW


# The palette, in rack order (docs/07). Each channel has a line in the project's notes, which
# get_scene and fl.channels show the model: the demo's models put chords on a bass synth when
# nothing said which channel was which (2026-10-28).
PALETTE = [
    "Drums 808", "Drums House", "Drums Acoustic", "Cowbell", "808", "Bass", "Synth Bass", "Piano",
    "E-Piano", "Strings", "Pad", "Choir", "Brass", "Woodwinds", "Sax", "Guitar", "Lead", "Pluck",
    "Synth 1", "Synth 2", "Sampler 1", "Sampler 2", "Sampler 3",
]


def test_every_palette_channel_says_what_it_is_for():
    names = [c.name for c in flp.read(template_path()).channels]
    guide = flp.project_guide(template_path(), names)

    assert names == PALETTE
    assert set(guide.about) == set(PALETTE)
    assert "Notes: 36 Kick Drum" in guide.about["Drums 808"]
    assert "MIDI 60" in guide.about["Cowbell"]
    assert "Pattern 1" in guide.notes and "Maximus" in guide.notes


def test_the_bridge_is_the_last_master_effect():
    effects = [flpkit.effects_at(template_path(), 0, slot=slot) for slot in range(10)]

    assert [e[0].name for e in effects if e][-1] == "FL MCP Bridge"
    assert [e.name for e in effects[9]] == ["FL MCP Bridge"]


def test_the_template_references_samples_by_fl_path_and_embeds_no_audio():
    """Legal to ship: every sample is FL's own factory content, found by FL in its install;
    no audio is copied into the project (docs/07 "What it contains")."""
    data = template_path().read_bytes()
    paths = [p.decode("utf-16-le") for p in re.findall(rb"%\x00F\x00L\x00(?:[\x20-\x7e]\x00){8,}", data)]
    paths += [p.decode("ascii") for p in re.findall(rb"%FL[\x20-\x7e]{8,}", data)]  # DirectWave's own paths

    assert paths and all(p.startswith(("%FLStudioFactoryData%", "%FLStudioData%")) for p in paths)
    assert b"RIFF" not in data and b"WAVEfmt" not in data and b"OggS" not in data


def test_the_template_does_not_hold_fl_at_its_project_info_on_open():
    assert codec.read(template_path(), ShowInfoFormat(), codec.Target()) == [False]


def test_template_console_script_prints_the_packaged_template_path_and_sha():
    path_result = subprocess.run(
        [_template_command()], capture_output=True, check=True, text=True
    )
    resolved = Path(path_result.stdout.strip())

    assert path_result.stdout == f"{resolved}\n"
    assert resolved.is_file()
    assert hashlib.sha256(resolved.read_bytes()).hexdigest() == TEMPLATE_SHA256

    sha_result = subprocess.run(
        [_template_command(), "--sha"], capture_output=True, check=True, text=True
    )
    assert sha_result.stdout == f"{TEMPLATE_SHA256}\n"


def test_make_agent_project_copies_the_packaged_template(monkeypatch, tmp_path):
    destination = tmp_path / "agent.flp"
    monkeypatch.setattr(make_agent_project, "destination", lambda: destination)

    assert make_agent_project.create(force=True) == destination
    assert destination.read_bytes() == template_path().read_bytes()
