from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from sysconfig import get_path

from fl_mcp import flp, make_agent_project
from fl_mcp.template import template_path

TEMPLATE_SHA256 = "ff110f543ab6d41557d2eba0500a0fd6d715f861b3a877757a0d424cabf9a669"


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
