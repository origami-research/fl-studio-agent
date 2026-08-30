"""Tier 2 finale: headless render of a cycled matrix project to a WAV.

`OsxFL -R song.flp` renders next to the project with FL CLOSED (experiment
E4, PASSED 2026-08-29 - Unix dash, not the Windows `/R`). This module runs
last in tests/e2e (alphabetical collection), after the module-scoped fl_host
of the live matrix has torn FL down.

Default renders only chord-progression (short song, real audio content);
widen with FL_E2E_RENDER=<comma-separated matrix ids>. A full demo song can
take minutes to render - budget the window accordingly (RUNBOOK_E2E.md).
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest
from harness import flctl

from e2e.matrix import MATRIX, run_file_cycle
from fl_mcp.flpaths import fl_app

pytestmark = pytest.mark.e2e

AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg")
RENDER_TIMEOUT = 240.0


def _selected() -> list:
    chosen = os.environ.get("FL_E2E_RENDER", "chord-progression")
    ids = [s.strip() for s in chosen.split(",") if s.strip()]
    known = {p.id for p in MATRIX}
    unknown = [i for i in ids if i not in known]
    assert not unknown, f"FL_E2E_RENDER names unknown projects {unknown}; known: {sorted(known)}"
    return [p for p in MATRIX if p.id in ids]


@pytest.fixture()
def render_gate():
    if not os.environ.get("FL_E2E"):
        pytest.skip("live-FL e2e disabled; set FL_E2E=1 to run")
    if not fl_app().exists():
        pytest.skip("FL Studio not installed")
    if flctl.is_running():
        flctl.stop(force=True)  # -R renders only with no GUI session (E4)
        time.sleep(1)
    yield
    flctl.stop(force=True)  # reap whatever -R launched (pgrep matches OsxFL)


@pytest.mark.parametrize("project", _selected(), ids=lambda p: p.id)
def test_headless_render_produces_wav(project, render_gate, tmp_path):
    outcome = run_file_cycle(project, tmp_path, tempo=180.0)

    binary = next(iter((fl_app() / "Contents/MacOS").glob("*")), None)
    assert binary is not None, "FL binary not found under Contents/MacOS"

    # render_gate's teardown reaps the process whatever happens below (pgrep
    # -f "FL Studio" matches the OsxFL binary's full path).
    subprocess.Popen(
        [str(binary), "-R", str(outcome.path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + RENDER_TIMEOUT
    produced = None
    while time.time() < deadline and produced is None:
        produced = next(
            (p for p in tmp_path.iterdir() if p.suffix.lower() in AUDIO_EXTS), None
        )
        time.sleep(2)
    assert produced is not None, (
        f"[{project.id} / step:headless-render] no audio file appeared next to "
        f"{outcome.path.name} within {RENDER_TIMEOUT:g}s"
    )
    # The file appears at render start; wait for its size to settle before
    # judging it, so a slow render is not misread as an empty one.
    size = produced.stat().st_size
    while time.time() < deadline:
        time.sleep(2)
        now = produced.stat().st_size
        if now == size and size > 0:
            break
        size = now
    assert size > 1000, (
        f"[{project.id} / step:headless-render] {produced.name} is {size} bytes - "
        "an empty render"
    )
