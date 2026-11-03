"""Which channels a render hears: solo some, mute some, then put FL back as it was.

A render with ``solo`` or ``mute`` sets each channel's mute in FL's channel rack
explicitly (``muteChannel(index, 1 or 0)``, never a toggle), one channel at a time,
and reads each one back. FL's own channel solo is not used: un-soloing unmutes every
channel, which would wipe the user's own mutes. Muting a channel silences what it plays and leaves every mixer
track, effect and send in place, so a soloed channel is heard through its whole
routing, as in the mix.

Afterwards exactly the channels it changed are changed back, read back again, also
when the render failed. A channel FL would not set back is an error naming it,
because the song would otherwise sound different from what the model believes.

FL records each mute change in its undo history (box run 2026-11-03: an undo right
after a solo render muted a channel again), and its history cannot be stepped
reliably from a script, so FLSession.undo guards against undoing one instead.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from fl_mcp import errors

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)


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
        self._now = dict(self.was)
        self._changed: set[int] = set()
        return self

    def hear(self, solo: Sequence[int], mute: Sequence[int] = ()) -> None:
        """Only ``solo`` (every channel as it was, when empty) sound, minus ``mute``."""
        want = {i: (i not in solo if solo else muted) or i in mute for i, muted in self.was.items()}
        for index in sorted(i for i in want if want[i] != self._now[i]):
            self._changed.add(index)  # before the call: a change FL applied but did not confirm is set back too
            set_muted(self._session, index, want[index])
            self._now[index] = want[index]

    def __exit__(self, exc_type: object, failed: BaseException | None, tb: object) -> None:
        stuck = []
        for index in sorted(self._changed):
            try:
                set_muted(self._session, index, self.was[index])
            except Exception as error:  # noqa: BLE001 - one channel must not stop the others being set back
                log.warning("could not restore channel %s's mute after a render: %s", index, error)
                stuck.append(index)
        if stuck:
            listed = ", ".join(f"{self.names[i]} is still {'unmuted' if self.was[i] else 'muted'}" for i in stuck)
            first = f" The render had already failed: {failed}" if failed is not None else ""
            raise errors.ToolError(
                "fl_api_error",
                f"FL did not set every channel back after the render: {listed}.{first}",
                "Set them back in FL's channel rack before the next render or check.",
            )


def set_muted(session: FLSession, index: int, muted: bool) -> None:
    """Set one channel's mute and confirm FL has the state asked for."""
    session.link.command("channels.muteChannel", [index, int(muted)])
    now = session.link.call("channels.isChannelMuted", [index])
    if bool(now) != muted:
        raise errors.fl_api_error("channels.muteChannel", f"channel {index} is still {'muted' if now else 'unmuted'}")
