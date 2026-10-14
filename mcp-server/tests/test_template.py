from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from sysconfig import get_path

from flpkit.codec import Stream
from flpkit.formats.playlist import EVENT_PLAYLIST, PlaylistFormat

from fl_mcp import flp, make_agent_project
from fl_mcp.template import template_path

TEMPLATE_SHA256 = "a080994a1b9d90b1e42b388e806d646f14f576f941ce2766ad43e7bf05daf2e4"

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

    assert project.playlist == (
        flp.FlpPlaylistItem(
            position=0, length=1536, track=1, pattern=1, channel=None, arrangement=0
        ),
    )


def test_template_clip_follows_pattern_one_length():
    data = template_path().read_bytes()
    (payload,) = [data[off : off + size] for event, off, size in Stream(data) if event == EVENT_PLAYLIST]
    (clip,) = PlaylistFormat().decode(payload, ppq=96)

    assert clip.pattern == 1
    assert clip.tail[8:16] == AUTO_LENGTH_WINDOW


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
