"""The provenance net: every capability carries how+when it was verified.

Enforces docs/30 Surface 5 as a regression gate, not prose: tags must be
internally consistent, must not be stale against the FL the profile says we
run, and a capability tagged "planned" must not silently ship a writer -
when one lands, this suite fails until its tag is promoted with evidence and
the format gets its fuzz row.
"""

from __future__ import annotations

from dataclasses import replace

import flpkit as flp
import pytest

from fl_mcp.core import provenance
from fl_mcp.profile import load_profile

# capability tagged planned -> the flpkit writer name whose appearance must
# force a tag promotion (DATA: extend when new planned capabilities register).
PLANNED_WRITERS = {
    "automation.write": "write_automation",
    "mixer.route": "write_mixer_routing",
}


def test_every_tag_is_internally_consistent():
    for tag in provenance.tags():
        assert tag.evidence, f"{tag.capability}: no evidence reference"
        if tag.status == "planned":
            assert tag.verified_fl is None, f"{tag.capability}: planned yet claims verification"
        else:
            assert tag.verified_fl, f"{tag.capability}: {tag.status} without a verified FL version"


def test_no_tag_is_stale_against_the_current_profile():
    current = load_profile().meta.fl_version
    stale = [t.capability for t in provenance.tags() if t.stale(current)]
    assert not stale, (
        f"tags verified against an older FL than the profile's {current}: {stale} - "
        "re-verify (runbook window) and update each tag's verified_fl/evidence"
    )


def test_planned_capabilities_ship_no_writer():
    for capability, writer in PLANNED_WRITERS.items():
        tag = provenance.lookup(capability)
        assert tag is not None and tag.status == "planned", f"{capability}: expected a planned tag"
        assert not hasattr(flp, writer), (
            f"flpkit.{writer} landed: promote {capability}'s CapabilityTag "
            "(status + verified_fl + evidence) and add its fuzz row"
        )


def test_registry_rejects_conflicting_claims():
    tag = provenance.lookup("notes.read")
    assert provenance.register(tag) is tag  # identical re-registration is fine
    with pytest.raises(ValueError, match="conflicting"):
        provenance.register(replace(tag, status="live"))
