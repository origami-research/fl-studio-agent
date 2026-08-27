"""Decompiler tests over the real .flp corpus (docs/18).

Decompile real projects into construction programs and assert the program
faithfully captures what the parser sees. Full round-trip (replay -> rebuild ->
diff) is an e2e concern (needs the write backends); this proves the read ->
program half on real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parents[1]
for p in (str(_TESTS), str(_TESTS.parent.parent), str(_TESTS.parent / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from corpus.fetch import fetch_all

_FETCHED = fetch_all()
pytestmark = pytest.mark.skipif(not _FETCHED, reason="corpus unavailable (offline?)")


def test_decompile_emits_tempo_channels_and_notes():
    name = "pyflp_multi_channel.flp"
    if name not in _FETCHED:
        pytest.skip(f"{name} not fetched")
    from fl_mcp.decompile import decompile, to_python

    prog = decompile(_FETCHED[name])
    ops = [s.op for s in prog.steps]

    # tempo first, then channels, then notes - the dependency order.
    assert ops[0] == "set_tempo"
    assert "add_channel" in ops
    assert "write_notes" in ops
    assert ops.index("add_channel") < ops.index("write_notes")  # channels before notes

    tempo_step = prog.steps[0]
    assert tempo_step.args["bpm"] == 140.0

    note_steps = [s for s in prog.steps if s.op == "write_notes"]
    assert note_steps, "no notes decompiled from a project that has them"
    for s in note_steps:
        for n in s.args["notes"]:
            assert 0 <= n["key"] <= 127
            assert 0.0 <= n["velocity"] <= 1.0
            assert n["length"] > 0

    # The Python rendering is syntactically valid.
    import ast
    ast.parse(to_python(prog))


def test_decompile_larger_project_has_all_its_channels():
    name = "pyflp_fl_20_8_4.flp"
    if name not in _FETCHED:
        pytest.skip(f"{name} not fetched")
    from fl_mcp.decompile import decompile
    from harness.flp_compat import import_pyflp, parse

    import_pyflp()
    project = parse(str(_FETCHED[name]))
    expected_channels = len(list(project.channels))

    prog = decompile(_FETCHED[name])
    add_channel_steps = [s for s in prog.steps if s.op == "add_channel"]
    assert len(add_channel_steps) == expected_channels
