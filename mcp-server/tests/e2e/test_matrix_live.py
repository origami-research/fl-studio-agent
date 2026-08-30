"""Tier 2: the live-FL matrix (THE ORCHESTRATOR RUNS IT - see RUNBOOK_E2E.md).

Run: FL_E2E=1 .venv/bin/python -m pytest tests/e2e -m e2e

Each matrix project goes through the identical tier-1 file cycle, then real
FL loads the result and testifies:

* the window title proves FL loaded OUR file (fact 5 - a rejected file
  leaves FL on a default project whose readbacks look plausible);
* the unique per-project tempo marker reads back via the control plane (the
  positive control - it also catches a silently-failed project switch,
  because the previous project's marker differs);
* on template projects, the channel volume/pan patch reads back through FL's
  own channels API (the plain-index API is proven for ungrouped templates;
  demo songs use display groups whose index mapping is unverified, so their
  live evidence stays title + tempo).

One FL boot serves the whole matrix (the ~20-minute window budget). Trim
with FL_E2E_MATRIX=<comma-separated ids> when the window is short.
"""

from __future__ import annotations

import os

import pytest

from e2e.matrix import CYCLE_PAN, CYCLE_VOLUME, MATRIX, run_file_cycle

pytestmark = pytest.mark.e2e


def _selected() -> list:
    chosen = os.environ.get("FL_E2E_MATRIX", "")
    if not chosen:
        return MATRIX
    ids = [s.strip() for s in chosen.split(",") if s.strip()]
    known = {p.id for p in MATRIX}
    unknown = [i for i in ids if i not in known]
    assert not unknown, f"FL_E2E_MATRIX names unknown projects {unknown}; known: {sorted(known)}"
    return [p for p in MATRIX if p.id in ids]


@pytest.mark.parametrize("project", _selected(), ids=lambda p: p.id)
def test_live_matrix_cycle(project, fl_host, tmp_path):
    tempo = 200.0 + MATRIX.index(project)  # unique marker per project
    outcome = run_file_cycle(project, tmp_path, tempo=tempo)

    fl_host.open(outcome.path)  # fails naming the file if no title ever shows

    got = fl_host.call("mixer.getCurrentTempo")
    assert got == round(tempo * 1000), (
        f"[{project.id} / step:live-tempo-readback] FL reports {got}, "
        f"wrote {round(tempo * 1000)} - FL is not looking at our file"
    )

    if outcome.levels_written and project.live_levels_readback:
        volume = fl_host.call("channels.getChannelVolume", outcome.channel)
        assert volume == pytest.approx(CYCLE_VOLUME, abs=0.01), (
            f"[{project.id} / step:live-levels-readback] channel {outcome.channel} "
            f"volume {volume!r}, wrote raw {round(CYCLE_VOLUME * 12800)}"
        )
        pan = fl_host.call("channels.getChannelPan", outcome.channel)
        # FL's pan API convention is unpinned: -1..1 echoes 0.25, 0..1 maps it
        # to 0.625. Either confirms the patch landed; centre or the stock
        # value would mean it did not.
        assert pan == pytest.approx(CYCLE_PAN, abs=0.01) or pan == pytest.approx(
            0.5 + CYCLE_PAN / 2, abs=0.01
        ), (
            f"[{project.id} / step:live-levels-readback] channel {outcome.channel} "
            f"pan {pan!r}, wrote raw {round(6400 + CYCLE_PAN * 6400)}"
        )
