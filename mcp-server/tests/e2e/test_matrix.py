"""Tier 1: the headless file-lane matrix (part of the default fast suite).

Runs the full cycle from e2e.matrix against every matrix project with no FL
process - anywhere FL's bundled Data dir exists. Tier 2
(test_matrix_live.py) reuses the identical cycle and adds real-FL readbacks.
"""

from __future__ import annotations

import pytest

from e2e.matrix import MATRIX, MATRIX_IDS, requires_fl_data, run_file_cycle


@requires_fl_data
@pytest.mark.parametrize("project", MATRIX, ids=MATRIX_IDS)
def test_file_lane_cycle(project, tmp_path):
    run_file_cycle(project, tmp_path, tempo=200.0 + MATRIX.index(project))
