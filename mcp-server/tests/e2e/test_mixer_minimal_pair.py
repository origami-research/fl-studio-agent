"""The promotion gate for a mixer writer.

This is deliberately an expected failure: the corpus establishes the event
shapes, but it does not prove that a public insert number maps to a particular
225 record or that id 192 is the fader value's scale.  E9 must produce the
FL-authored pair before a Format (and therefore a writer) may be added.  The
pair is an owned artifact, not an assertion in a PR description: both saved
FLPs must be checked in here before this gate can be promoted.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

_EVIDENCE = Path(__file__).with_name("evidence") / "mixer-volume"
_BASELINE = _EVIDENCE / "baseline.flp"
_CHANGED = _EVIDENCE / "changed.flp"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing FL-authored E9 volume minimal pair; capture baseline -> "
        "mixer.setTrackVolume(1, 0.73) -> saved project, then prove the one "
        "changed id-192 record and public-index mapping"
    ),
)
def test_mixer_volume_writer_requires_an_fl_authored_minimal_pair():
    assert _BASELINE.is_file(), (
        "missing FL-authored baseline: run "
        "experiments/e9_mixer_minimal_pair.py --op volume --value 0.73 "
        "with FL frontmost and check in the resulting baseline.flp"
    )
    assert _CHANGED.is_file(), "missing FL-authored changed.flp beside the baseline"
    assert _BASELINE.read_bytes() != _CHANGED.read_bytes(), "minimal-pair saves are byte-identical"
