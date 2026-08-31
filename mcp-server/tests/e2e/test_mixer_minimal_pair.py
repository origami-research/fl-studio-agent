"""The promotion gate for a mixer writer.

This is deliberately an expected failure: the corpus establishes the event
shapes, but it does not prove that a public insert number maps to a particular
225 record or that id 192 is the fader value's scale.  E9 must produce the
FL-authored pair before a Format (and therefore a writer) may be added.
"""

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing FL-authored E9 volume minimal pair; capture baseline -> "
        "mixer.setTrackVolume(1, 0.73) -> saved project, then prove the one "
        "changed id-192 record and public-index mapping"
    ),
)
def test_mixer_volume_writer_requires_an_fl_authored_minimal_pair():
    pytest.fail(
        "No verified mixer-volume byte contract: run "
        "experiments/e9_mixer_minimal_pair.py --op volume --value 0.73 "
        "with FL frontmost, then attach and decode the pair."
    )
