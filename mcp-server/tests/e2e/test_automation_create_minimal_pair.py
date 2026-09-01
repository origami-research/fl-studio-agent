"""Promotion gate for new automation clips and their target links.

Existing-clip point replacement is verified separately.  A creator needs an
FL-authored one-change pair that proves both the new channel/event set and the
target link, so this stays an expected failure until that evidence is checked
in and decoded.
"""

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing FL-authored automation-creation minimal pair: save Empty -> "
        "Create automation clip for one stable parameter -> save, then decode "
        "the new channel, event-234 blob, and target-link events"
    ),
)
def test_automation_creator_requires_an_fl_authored_minimal_pair():
    pytest.fail(
        "No verified automation-creation byte contract: capture the FL-authored "
        "pair and prove its target link before adding AutomationFormat.create()."
    )
