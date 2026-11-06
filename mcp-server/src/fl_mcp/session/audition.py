"""Which channels a render hears: solo some, mute some, then put FL back as it was.

A render with ``solo`` or ``mute`` sets channel mutes in FL's channel rack explicitly
(``muteChannel(index, 1 or 0)``, never a toggle, so setting one twice is harmless), all
of them in one controller op (``ctl.set_mutes``) that reads every one back, then waits
until FL answers again before anything plays. One op each way, not one per channel: a
solo render on the 23-channel agent project sent 44 requests around playback, and FL
stopped answering midway, leaving 22 channels muted (#309). FL's own channel solo is
not used: un-soloing unmutes every channel, which would wipe the user's own mutes.
Muting a channel silences what it plays and leaves every mixer track, effect and send
in place, so a soloed channel is heard through its whole routing, as in the mix. (A
routing solo, recording chosen mixer tracks, would not touch mutes, but channels that
share a track, or play straight into the master as the agent project's do, cannot be
told apart that way.)

Afterwards exactly the channels it changed are changed back in one op and read back,
also when the render failed. If FL does not answer, the restore waits up to
RESTORE_WAIT_SECONDS for it and sends the same op again; if FL still does not answer,
the error names every channel that may still be muted or unmuted, and the session sets
them back before its next solo or mute render, because the song would otherwise sound
different from what the model believes.

FL records each mute change in its undo history (box run 2026-11-03: an undo right
after a solo render muted a channel again), and its history cannot be stepped
reliably from a script, so FLSession.undo guards against undoing one instead.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

from fl_mcp import errors

from .link import LIVE_SET_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

# How long FL may take to answer after the mutes change before the render gives up
# without playing: FL answers in milliseconds when it is well.
ANSWER_BEFORE_PLAY_SECONDS = 10.0
# How long a restore FL did not answer waits for FL before sending it once more; the
# session reports FL as stopped after 30 s of silence (link.STOPPED_AFTER_SECONDS).
RESTORE_WAIT_SECONDS = 30.0


@contextlib.contextmanager
def audition(session: FLSession, solo: Sequence[int], mute: Sequence[int]) -> Iterator[None]:
    """Inside the block only ``solo`` channels (all when empty) sound, minus ``mute``."""
    if not solo and not mute:
        yield
        return
    both = sorted(set(solo) & set(mute))
    if both:
        raise errors.invalid_argument("fl.render", f"channel {', '.join(map(str, both))} is in both solo and mute")
    with Rack(session) as rack:
        unknown = sorted((set(solo) | set(mute)) - set(rack.was))
        if unknown:
            raise errors.invalid_argument("fl.render", f"no channel at index {', '.join(map(str, unknown))}")
        rack.hear(solo, mute)
        yield


class Rack:
    """FL's channel mutes for the length of one or more renders: ``hear`` sets what a render
    needs, changing only the channels that differ, and leaving the block sets back every
    channel it changed (stems play one channel after another, two changes apart)."""

    def __init__(self, session: FLSession) -> None:
        self._session = session

    def __enter__(self) -> Rack:
        channels = self._session.snapshot().channels
        self.names = {c.index: c.name for c in channels}
        self.was = {c.index: c.muted for c in channels}
        self._put_back_unrestored()
        self._now = dict(self.was)
        self._changed: set[int] = set()
        return self

    def _put_back_unrestored(self) -> None:
        """Set back what an earlier render could not, on the channels that still have the
        same index and name and are not already as they were."""
        owed = self._session.unrestored_mutes
        due = {i: muted for (i, name), muted in owed.items() if self.names.get(i) == name and self.was[i] != muted}
        if due:
            log.info("setting back channel mutes an earlier render left: %s", sorted(due))
            set_mutes(self._session, due)
            self.was.update(due)
        owed.clear()

    def hear(self, solo: Sequence[int], mute: Sequence[int] = ()) -> None:
        """Only ``solo`` (every channel as it was, when empty) sound, minus ``mute``; returns
        once FL has every mute and answers again, so nothing plays into a busy FL."""
        want = {i: (i not in solo if solo else muted) or i in mute for i, muted in self.was.items()}
        change = {i: m for i, m in want.items() if m != self._now[i]}
        if not change:
            return
        self._changed.update(change)  # before the op: a change FL applied but did not confirm is set back too
        set_mutes(self._session, change)
        self._now.update(change)
        try:
            self._session.link.call("ping", timeout=ANSWER_BEFORE_PLAY_SECONDS)
        except errors.ToolError as exc:
            raise errors.ToolError(
                "render_failed",
                f"FL did not answer after the channel mutes changed, so nothing was played: {exc.message}",
                "Retry the render once FL answers again.",
                retryable=True,
            ) from exc

    def __exit__(self, exc_type: object, failed: BaseException | None, tb: object) -> None:
        if not self._changed:
            return
        back = {i: self.was[i] for i in self._changed}
        error = _restore(self._session, back)
        if error is None:
            return
        self._session.unrestored_mutes.update({(i, self.names[i]): m for i, m in back.items()})
        raise unrestored(back, self.names, error, failed)


def _restore(session: FLSession, back: Mapping[int, bool]) -> errors.ToolError | None:
    """Set ``back`` in one op; when FL does not take it, wait for FL to answer and send it
    once more. The last failure, or None when FL confirmed every channel."""
    try:
        set_mutes(session, back)
    except errors.ToolError as first:
        log.warning("could not set the channel mutes back after a render; waiting for FL: %s", first.message)
    else:
        return None
    try:
        session.link.call("ping", timeout=RESTORE_WAIT_SECONDS)
    except errors.ToolError as exc:
        log.warning("FL did not answer within %gs of a failed mute restore: %s", RESTORE_WAIT_SECONDS, exc.message)
    try:
        set_mutes(session, back)
    except errors.ToolError as again:
        log.warning("FL did not set the channel mutes back after a render: %s", again.message)
        return again
    return None


def unrestored(
    back: Mapping[int, bool], names: Mapping[int, str], error: errors.ToolError, failed: BaseException | None
) -> errors.ToolError:
    """The error naming exactly the channels FL may still have muted or unmuted."""
    stuck = [
        f" These may still be {'muted' if muted else 'unmuted'}: "
        + ", ".join(f"{names[i]} ({i})" for i in sorted(back) if back[i] != muted)
        + "."
        for muted in (True, False)
        if any(m != muted for m in back.values())
    ]
    first = f" The render had already failed: {failed}" if failed is not None else ""
    return errors.ToolError(
        "fl_api_error",
        f"FL did not confirm the channel mutes were set back after the render ({error.message})."
        f"{''.join(stuck)}{first}",
        "The next solo or mute render sets them back first, once FL answers; until then the song "
        "may sound different. If FL stays silent, set them back in FL's channel rack.",
    )


def set_mutes(session: FLSession, wanted: Mapping[int, bool]) -> None:
    """Set these channels' mutes in one controller op and confirm FL has every one."""
    pairs = [[index, int(muted)] for index, muted in sorted(wanted.items())]
    answer = session.link.call("ctl.set_mutes", [pairs], timeout=LIVE_SET_TIMEOUT_SECONDS)
    try:
        now = {int(index): bool(muted) for index, muted in answer}
    except (TypeError, ValueError) as exc:
        raise errors.fl_api_error("ctl.set_mutes", f"malformed answer {answer!r}") from exc
    wrong = sorted(i for i, muted in wanted.items() if now.get(i) != muted)
    if wrong:
        states = ", ".join(f"{i} is {'muted' if now.get(i) else 'unmuted'}" for i in wrong)
        raise errors.fl_api_error("ctl.set_mutes", f"FL did not take every mute: channel {states}")
