"""One budget bounds a run's wall time, fl calls included (#333).

A cell of slow renders outlasted the arena harness's 900 s per call because time
inside fl calls was not counted, and the run got no answer at all. These tests hold
the parts that need no kernel: the setting, and the refusal a real-time call asks for
before it starts. test_kernel.py holds the interrupt and the refusals end to end.
"""

from __future__ import annotations

import time

import pytest

from fl_mcp import budget
from fl_mcp.contract import ENV_RUN_BUDGET
from fl_mcp.errors import ToolError
from fl_mcp.kernel.host import RUN_BUDGET_SECONDS, run_budget_from_env


def test_the_default_budget_leaves_room_under_the_harness_limit():
    """The arena harness gives each tool call 900 s; past the budget a run still finishes
    the call in flight, the interrupt and an open file_edit block (FL reopens the song)."""
    assert RUN_BUDGET_SECONDS <= 900 - 300
    assert run_budget_from_env({}) == RUN_BUDGET_SECONDS


def test_a_host_sets_its_own_budget():
    assert run_budget_from_env({ENV_RUN_BUDGET: "240"}) == 240.0


@pytest.mark.parametrize("value", ["", "soon", "0", "-5", "inf", "nan"])
def test_a_bad_budget_is_a_setup_error(value):
    with pytest.raises(ValueError, match=ENV_RUN_BUDGET):
        run_budget_from_env({ENV_RUN_BUDGET: value})


def test_outside_a_run_nothing_is_bounded():
    budget.require(1e9, "This render", "render fewer bars")


def test_a_call_that_fits_what_is_left_starts():
    with budget.serving(budget.RunBudget.starting_now(60.0)):
        budget.require(30.0, "This render", "render fewer bars")


def test_a_call_longer_than_what_is_left_is_refused_with_how_to_split_it():
    partly_spent = budget.RunBudget(60.0, time.monotonic() + 20.0)
    with budget.serving(partly_spent), pytest.raises(ToolError) as refused:
        budget.require(30.0, "This render", "render fewer bars")
    assert refused.value.category == "run_budget_exceeded"
    assert refused.value.message.startswith("This render takes about 30 s, but this run has ")
    assert "left of its 60 s budget, so FL was not asked to start it." in refused.value.message
    assert refused.value.fix.endswith("or render fewer bars.")


def test_a_call_longer_than_a_whole_run_says_only_how_to_shrink_it():
    with budget.serving(budget.RunBudget.starting_now(240.0)), pytest.raises(ToolError) as refused:
        budget.require(300.0, "An offline export", "record the song with fl.render instead")
    assert refused.value.fix == "It does not fit in any run here: record the song with fl.render instead."


def test_serving_ends_with_its_call():
    with budget.serving(budget.RunBudget.starting_now(0.0)):
        pass
    budget.require(1e9, "This render", "render fewer bars")


def test_a_spent_budget_refuses_any_call():
    spent = budget.spent(budget.RunBudget.starting_now(600.0))
    assert spent.category == "run_budget_exceeded"
    assert "used its 600 s budget" in spent.message
