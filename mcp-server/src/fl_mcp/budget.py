"""The time an fl call may take: what is left of the run serving it.

The kernel host bounds each run's wall time, fl calls included
(kernel/host.py RUN_BUDGET_SECONDS), and serves every fl call under
``serving``. A call that plays in real time knows how long it will play before
it starts, so it calls ``require`` first and is refused, with FL untouched, when
it would run past the run's end; a long computation (stem_model.py) calls it
between steps with its estimate of the rest. Outside a run (the dev session,
unit tests of the session) nothing is bounded.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from fl_mcp import errors


@dataclass(frozen=True)
class RunBudget:
    """One run's wall-time budget: ``seconds`` in all, ending at ``deadline`` (time.monotonic)."""

    seconds: float
    deadline: float

    @classmethod
    def starting_now(cls, seconds: float) -> RunBudget:
        return cls(seconds, time.monotonic() + seconds)

    def left(self) -> float:
        return self.deadline - time.monotonic()


_serving: ContextVar[RunBudget | None] = ContextVar("fl_mcp_run_budget", default=None)


@contextlib.contextmanager
def serving(budget: RunBudget) -> Iterator[None]:
    """Serve one fl call within ``budget``; the session's real-time calls check it."""
    token = _serving.set(budget)
    try:
        yield
    finally:
        _serving.reset(token)


def require(seconds: float, what: str, split: str, outcome: str = "so FL was not asked to start it") -> None:
    """Refuse ``what`` before it starts when it takes ``seconds`` and the run has less left.

    ``split`` says how to make the work smaller, for the error's fix; ``outcome`` what
    the refusal left undone.
    """
    budget = _serving.get()
    if budget is None:
        return
    left = budget.left()
    if seconds <= left:
        return
    if seconds > budget.seconds:
        fix = f"It does not fit in any run here: {split}."
    else:
        fix = f"Split the work: end this run here and start the next one with it (variables are kept), or {split}."
    raise errors.ToolError(
        "run_budget_exceeded",
        f"{what} takes about {seconds:.0f} s, but this run has {max(left, 0.0):.0f} s left of its "
        f"{budget.seconds:g} s budget, {outcome}.",
        fix,
    )


def did_not_run(budget: RunBudget, why: str) -> errors.ToolError:
    """The refusal for a run whose budget was gone before its cell could start."""
    return errors.ToolError(
        "run_budget_exceeded",
        f"This code did not run: {why} this run's whole {budget.seconds:g} s budget.",
        "Run it again; nothing in FL or the kernel changed.",
        retryable=True,
    )


def spent(budget: RunBudget) -> errors.ToolError:
    """The refusal for an fl call that reaches the server after its run's budget is spent."""
    return errors.ToolError(
        "run_budget_exceeded",
        f"This run used its {budget.seconds:g} s budget, so FL was not asked to do this; the cell is being interrupted.",
        "Continue in the next run (variables are kept); actions lists what FL already did.",
    )
