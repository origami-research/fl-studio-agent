"""Starting and stopping FL's playback for a render or fl.meters.

FL plays only inside these two, and both follow the same rules. Nothing starts
until FL answers a ping, so a render or a meter sweep never begins in an FL that is
still busy with the requests before it (#309, #317). The stop afterwards is sent
again once FL answers when FL missed it, so a busy moment does not leave FL playing.
While FL plays, the session sends as few requests as it can: a meter sweep is one
``state.peaks`` op for every track, and a render sends none.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from fl_mcp import errors

from .link import STOPPED_AFTER_SECONDS

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

# How long FL may take to answer before playback, after which nothing is played:
# FL answers in milliseconds when it is well.
ANSWER_BEFORE_PLAY_SECONDS = 10.0
# How long a change FL did not answer (a stop, a mute restore) waits for FL before it is
# sent once more: as long as the session waits before it reports FL as stopped.
WAIT_FOR_FL_SECONDS = STOPPED_AFTER_SECONDS


def confirm_answering(session: FLSession, category: str, what: str) -> None:
    """Return once FL answers a ping; otherwise raise ``category``, saying nothing was played."""
    try:
        session.link.call("ping", timeout=ANSWER_BEFORE_PLAY_SECONDS)
    except errors.ToolError as exc:
        raise errors.ToolError(
            category,
            f"FL did not answer before {what}, so nothing was played: {exc.message}",
            f"Retry the {what} once FL answers again.",
            retryable=True,
        ) from exc


def again_once_answering(session: FLSession, send: Callable[[], object], what: str) -> errors.ToolError | None:
    """Send an idempotent change; when FL does not take it, wait up to WAIT_FOR_FL_SECONDS
    for FL to answer and send it once more. The last failure, or None once FL took it."""
    try:
        send()
    except errors.ToolError as first:
        log.warning("FL did not take %s; waiting for FL to answer: %s", what, first.message)
    else:
        return None
    try:
        session.link.call("ping", timeout=WAIT_FOR_FL_SECONDS)
    except errors.ToolError as exc:
        log.warning("FL did not answer within %gs after missing %s: %s", WAIT_FOR_FL_SECONDS, what, exc.message)
    try:
        send()
    except errors.ToolError as again:
        log.warning("FL did not take %s when sent again: %s", what, again.message)
        return again
    return None


def stop(session: FLSession) -> errors.ToolError | None:
    """Stop FL's transport, sent again once FL answers if FL missed it (a repeated stop
    changes nothing). The last failure, or None once FL took it."""
    return again_once_answering(session, lambda: session.link.command("transport.stop"), "transport.stop")
