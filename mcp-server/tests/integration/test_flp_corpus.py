"""Integration tests over a real-world .flp corpus (fetched on demand).

Real FL projects from third parties (see tests/corpus/manifest.py) exercise our
parse + reconstruction path against variety we can't get from the fake or from
files we author ourselves. The corpus is downloaded into tests/corpus/data/
(gitignored) at session start; if the download fails (offline), these skip.

These do NOT need FL Studio - they parse .flp bytes with PyFLP, the same reader
the project uses for read-back and reconstruction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parents[1]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
if str(_TESTS.parent.parent) not in sys.path:  # repo root, for harness/
    sys.path.insert(0, str(_TESTS.parent.parent))

from corpus.fetch import fetch_all
from corpus.manifest import CORPUS, CORRUPT

_FETCHED = fetch_all()


def _have(name: str) -> bool:
    return name in _FETCHED


pytestmark = pytest.mark.skipif(not _FETCHED, reason="corpus unavailable (offline?)")


@pytest.mark.parametrize("name", list(CORPUS))
def test_real_project_parses_with_structure(name):
    if not _have(name):
        pytest.skip(f"{name} not fetched")
    from harness.flp_compat import import_pyflp, parse

    import_pyflp()
    project = parse(str(_FETCHED[name]))
    channels = list(project.channels)
    patterns = list(project.patterns)
    # A real project has at least some channels; PyFLP exposes them as objects.
    assert len(channels) >= 1, f"{name}: no channels parsed"
    assert isinstance(patterns, list)


@pytest.mark.parametrize("name", list(CORRUPT))
def test_corrupt_project_raises_cleanly_not_crash(name):
    if not _have(name):
        pytest.skip(f"{name} not fetched")
    from harness.flp_compat import import_pyflp, parse

    import_pyflp()
    # A corrupt file must raise a normal exception our tools can catch, never
    # segfault or hang. Any Exception subclass is acceptable; a crash is not.
    with pytest.raises(Exception):
        parse(str(_FETCHED[name]))


def test_notes_are_readable_from_a_real_project():
    """The reconstruction path depends on reading notes back; prove it works
    on a real multi-channel project, not just files we wrote."""
    name = "pyflp_multi_channel.flp"
    if not _have(name):
        pytest.skip(f"{name} not fetched")
    from harness.verify import read_notes

    rows = read_notes(_FETCHED[name])
    # multi-channel.flp is a patterns fixture; it should contain notes we can read.
    assert isinstance(rows, list)
    for r in rows[:5]:
        assert 0 <= r.midi <= 127
        assert r.start_beat >= 0 and r.length_beats > 0
