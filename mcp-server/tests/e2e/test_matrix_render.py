"""Tier 2 finale - the compose -> arrange -> render capability regression.

The cycle composes (notes) and arranges (clip + tempo + levels) through the
file lane, then `OsxFL -R song.flp` renders next to the project with FL
CLOSED (experiment E4, PASSED 2026-08-29 - Unix dash, not the Windows `/R`).
The golden is derived from the file itself: FL must produce real audio at
least as long as the arrangement we wrote says the song is. This module runs
last in tests/e2e (alphabetical collection), after the module-scoped fl_host
of the live matrix has torn FL down.

Default renders only chord-progression (short song, real audio content);
widen with FL_E2E_RENDER=<comma-separated matrix ids>. A full demo song can
take minutes to render - budget the window accordingly (RUNBOOK_E2E.md).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import wave

import flpkit as flp
import pytest
from harness import flctl

from e2e.matrix import run_file_cycle, selected
from fl_mcp.audio import analyze_wav
from fl_mcp.fl.paths import fl_app
from fl_mcp.schemas import Note

pytestmark = pytest.mark.e2e

AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg")
RENDER_TIMEOUT = 240.0


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


@pytest.mark.parametrize("project", selected("FL_E2E_RENDER", "chord-progression"), ids=lambda p: p.id)
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

    if produced.suffix.lower() == ".wav":
        with contextlib.closing(wave.open(str(produced))) as w:
            seconds = w.getnframes() / w.getframerate()
        arranged = flp.read(outcome.path)
        clips = [i for i in arranged.playlist if i.arrangement == project.write_arrangement]
        song_beats = max(i.position + i.length for i in clips) / arranged.ppq
        expected = song_beats * 60 / outcome.tempo
        assert seconds >= expected * 0.9, (
            f"[{project.id} / step:headless-render] rendered {seconds:.1f}s but the "
            f"arrangement we wrote spans {expected:.1f}s - the render ignored our clips"
        )


def test_bridge_template_pattern_one_renders_audible_audio(render_gate, tmp_path):
    """Pattern 1 is pre-placed so its normal note write reaches Song renders."""
    from fl_mcp.template import template_path

    template = template_path()
    project = tmp_path / template.name
    shutil.copy2(template, project)
    flp.write_notes(
        project,
        [Note(key=60, start=beat, length=0.25, velocity=1.0, pan=0.5) for beat in range(4)],
        pattern=1,
        channel=0,
        mode="replace",
    )
    assert any(item.pattern == 1 for item in flp.read(project).playlist)

    binary = next(iter((fl_app() / "Contents/MacOS").glob("*")), None)
    assert binary is not None, "FL binary not found under Contents/MacOS"
    subprocess.run([str(binary), "-R", str(project)], check=True)

    analysis = analyze_wav(project.with_suffix(".wav"))
    assert analysis.integrated_lufs > -120.0
    assert analysis.true_peak_dbfs > -240.0
