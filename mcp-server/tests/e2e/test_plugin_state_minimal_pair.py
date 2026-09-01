"""Promotion gate for plugin instance and preset file writers."""

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing FL-authored plugin minimal pairs: save Empty -> add FL Keys; "
        "save 3x Osc -> load Bell.fst; save 3x Osc -> add Fruity Parametric EQ 2. "
        "Each pair must identify every changed event before flpkit gains a writer."
    ),
)
def test_plugin_instance_and_preset_writers_require_fl_authored_minimal_pairs() -> None:
    pytest.fail(
        "The installed corpus proves opaque 201/212/213 placement, not an add/load "
        "byte contract. Capture and decode the three one-action FL-authored pairs first."
    )
