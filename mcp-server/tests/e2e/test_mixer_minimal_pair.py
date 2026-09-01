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
from flpkit.codec import Stream, chunks

pytestmark = pytest.mark.e2e

_EVIDENCE = Path(__file__).with_name("evidence") / "mixer-volume"
_BASELINE = _EVIDENCE / "baseline.flp"
_CHANGED = _EVIDENCE / "changed.flp"
_MIXER_PARAMETERS = 225
_RECORD_SIZE = 12


def _parameter_records(path: Path) -> tuple[bytes, int, list[bytes]]:
    """Return the one mapped mixer-parameter event as 12-byte records.

    This uses flpkit's canonical event walker; it deliberately does not
    invent a mixer decoder while the public writer remains evidence-locked.
    """
    data = path.read_bytes()
    header, raw = chunks(data)
    stream = Stream(raw)
    events = [
        (offset, bytes(stream[offset : offset + size]))
        for event_id, offset, size in stream
        if event_id == _MIXER_PARAMETERS
    ]
    assert len(events) == 1, f"expected one mixer parameter event, got {len(events)}"
    (payload_at, payload), = events
    assert len(payload) % _RECORD_SIZE == 0, (
        f"mixer parameter payload is {len(payload)} bytes, not whole {_RECORD_SIZE}-byte records"
    )
    return (
        data,
        16 + len(header) + payload_at,
        [payload[offset : offset + _RECORD_SIZE] for offset in range(0, len(payload), _RECORD_SIZE)],
    )


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

    baseline_data, baseline_at, baseline = _parameter_records(_BASELINE)
    changed_data, changed_at, changed = _parameter_records(_CHANGED)
    assert len(changed) == len(baseline), "volume edit changed the mixer parameter record count"
    differing = [(index, before, after) for index, (before, after) in enumerate(zip(baseline, changed)) if before != after]
    assert len(differing) == 1, f"volume pair changed {len(differing)} mixer parameter records, not one"
    index, before, after = differing[0]
    assert before[4] == after[4] == 192, "changed mixer parameter is not candidate volume id 192"
    assert before[:8] == after[:8], "volume pair changed parameter identity, not only its value"
    assert before[8:] != after[8:], "volume pair did not change the mapped i32 value field"
    assert len(changed_data) == len(baseline_data), "volume edit changed the FLP's byte length"
    changed_bytes = [
        offset for offset, (before, after) in enumerate(zip(baseline_data, changed_data)) if before != after
    ]
    value_field = range(baseline_at + index * _RECORD_SIZE + 8, baseline_at + index * _RECORD_SIZE + 12)
    assert set(changed_bytes).issubset(value_field), "volume pair changed bytes outside the mapped i32 value field"
    assert baseline_at == changed_at, "volume edit moved the mixer parameter event"
