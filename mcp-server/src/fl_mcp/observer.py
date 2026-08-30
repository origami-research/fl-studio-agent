"""The observer: FL user actions as one typed event stream (docs/27 §5).

Two sources feed one vocabulary, both LAZY - read when a caller asks, never a
realtime loop (owner directive; VERDICT Q4 makes realtime affordable, but
nothing consumes it):

- ``drain_pushes()`` tails ``bridge_events.jsonl`` under the spool roots -
  the FL push callbacks the native bridge forwards (docs/27 §6) - and maps
  them to ``UserAction``. These are FL's own 0-latency notifications
  (VERDICT Q3): play/stop, song position, focus, context, routing.
- ``poll()`` is the score-edit checkpoint. Score edits have NO push and NO
  live readback (VERDICT Q1/Q3), so when the project file's mtime advances
  (the user hit Cmd+S), the file is parsed ONCE and every (pattern, channel)
  is diffed against the last-seen baseline; each non-empty delta becomes a
  ``score_edit`` event. Between saves a user's edit is invisible by
  hardware fact, not by choice - the docstrings keep saying so.

The concurrency rule the note lanes consult (docs/27 §5): ``guard_write``
refuses - with a retryable ToolError - while a fresh user ``score_edit``
touched the same target within ``GUARD_WINDOW_SECONDS``. The lane reports its
own verified writes through ``record_agent_write`` so the agent's effects are
never presented (or guarded) as user activity.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from pydantic import ValidationError

from fl_mcp import errors, flp
from fl_mcp.clock import now
from fl_mcp.schemas import Note, NoteDelta, Target
from fl_mcp.spool import spool_roots
from fl_mcp.store import diff_notes

log = logging.getLogger(__name__)

# The bridge appends one JSON object per line (docs/27 §6); we tail it.
BRIDGE_EVENTS_FILE = "bridge_events.jsonl"

# How long after a user score_edit an agent write to the same target defers.
GUARD_WINDOW_SECONDS = 5.0

# Push events older than this ring are forgotten for de-duplication purposes;
# far larger than the bridge's 256-slot event ring, so a seq can only repeat
# across roots or re-reads, both of which land well inside the window.
_SEEN_SEQ_CAPACITY = 4096

UserActionKind = Literal[
    "transport", "position", "selection", "focus", "context", "routing", "score_edit"
]

_KINDS: frozenset[str] = frozenset(
    ("transport", "position", "selection", "focus", "context", "routing", "score_edit")
)
_PUSH_KINDS: frozenset[str] = _KINDS - {"score_edit"}

# FL push callback -> event kind (docs/27 §5; ids live-confirmed, VERDICT Q3
# via probe_events.jsonl). FPD_SetFocus carries both selection and focus in
# FL's model; without a discriminator it maps to "focus" - "selection" stays
# in the vocabulary for the day a discriminating surface exists.
# FPD_SetTimeSig / FPD_ProjLoaded are forwarded context changes with no kind
# of their own; the detail's "callback" keeps them distinguishable.
_PUSH_BY_ID: dict[int, tuple[str, UserActionKind]] = {
    12: ("FPD_SetPlaying", "transport"),
    13: ("FPD_SongPosChanged", "position"),
    22: ("FPD_SetFocus", "focus"),
    41: ("FPD_ContextInfoChanged", "context"),
    25: ("FPD_RoutingChanged", "routing"),
    14: ("FPD_SetTimeSig", "context"),
    27: ("FPD_ProjLoaded", "context"),
}
_PUSH_BY_NAME: dict[str, UserActionKind] = {name: kind for name, kind in _PUSH_BY_ID.values()}


@dataclass(frozen=True)
class UserAction:
    """One user-visible FL action - THE frozen event type (docs/27 §5).

    ``detail`` is kind-specific and validated at construction; a
    ``score_edit`` always carries an addressable target (``pattern`` >= 1,
    ``channel`` >= 0) and a ``NoteDelta`` under ``delta``, because the
    concurrency guard keys on exactly those fields.
    """

    kind: UserActionKind
    at: datetime
    detail: dict[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown UserAction kind {self.kind!r}; expected one of {sorted(_KINDS)}")
        if not isinstance(self.detail, dict):
            raise TypeError(f"UserAction detail must be a dict, got {type(self.detail).__name__}")
        # A private copy: frozen semantics end at the field reference, so the
        # one mutable member is decoupled from whatever the caller passed in.
        object.__setattr__(self, "detail", dict(self.detail))
        if self.kind == "score_edit":
            pattern, channel = self.detail.get("pattern"), self.detail.get("channel")
            if not (isinstance(pattern, int) and pattern >= 1):
                raise ValueError(f"score_edit detail needs an int pattern >= 1, got {pattern!r}")
            if not (isinstance(channel, int) and channel >= 0):
                raise ValueError(f"score_edit detail needs an int channel >= 0, got {channel!r}")
            if not isinstance(self.detail.get("delta"), NoteDelta):
                raise ValueError("score_edit detail needs a NoteDelta under 'delta'")


class UserEditing(errors.ToolError):
    """A fresh user score edit touched the write's target; defer or merge."""

    def __init__(self, target: Target, action: UserAction, window: float) -> None:
        super().__init__(
            "operation_unsafe",
            f"The user edited pattern {target.pattern} channel {target.channel} "
            f"within the last {window:g}s; refusing to clobber their work.",
            "Wait for the user to pause and retry (the guard clears after "
            f"{window:g}s), re-read the target to fold their edit in, or use "
            "mode='merge' if the write is purely additive.",
            retryable=True,
        )
        self.target = target
        self.action = action


class Observer:
    """The docs/27 §5 Observer: push drain + lazy checkpoint poll + guard.

    All reads happen inside the caller's thread when a method is invoked;
    there is no background loop to start or stop. State is lock-guarded
    because MCP dispatches tool calls on concurrent threads.
    """

    def __init__(
        self,
        project: Path | None = None,
        *,
        roots: list[Path] | None = None,
        clock: Callable[[], datetime] = now,
    ) -> None:
        """``project=None`` means no score to diff (poll returns []); ``roots``
        is the spool seam (tmp_path in tests), defaulting to the real roots."""
        self._project = project
        self._roots = list(roots) if roots is not None else spool_roots()
        self._clock = clock
        self._lock = threading.RLock()
        self._offsets: dict[Path, int] = {}
        self._seen_seqs: set[int] = set()
        self._seen_ring: deque[int] = deque(maxlen=_SEEN_SEQ_CAPACITY)
        # None until the first poll SEEDS it: pre-existing notes are state,
        # not actions, so the first read never emits events.
        self._baseline: dict[tuple[int, int], list[Note]] | None = None
        self._baseline_mtime_ns: int | None = None
        self._recent_edits: deque[UserAction] = deque(maxlen=256)

    # -- push events (bridge -> jsonl -> here) ----------------------------------

    def drain_pushes(self) -> list[UserAction]:
        """New forwarded FL callbacks since the last drain, mapped to
        UserAction. Unknown callbacks and torn/corrupt lines are skipped (and
        logged), never raised - a foreign line must not wedge the stream."""
        actions: list[UserAction] = []
        with self._lock:
            for root in self._roots:
                for record in self._tail(root / BRIDGE_EVENTS_FILE):
                    action = self._action_from_push(record)
                    if action is not None:
                        actions.append(action)
        return actions

    def _tail(self, path: Path) -> Iterator[dict[str, Any]]:
        """Complete new lines since the last read of this file. A shrunken
        file means the bridge restarted: start over from byte 0. A tail
        without a newline is a torn append: leave it for the next drain."""
        try:
            size = path.stat().st_size
        except OSError:
            return
        offset = self._offsets.get(path, 0)
        if size < offset:
            offset = 0
        if size == offset:
            return
        try:
            with path.open("rb") as f:
                f.seek(offset)
                blob = f.read()
        except OSError:
            log.warning("could not read %s; will retry on the next drain", path)
            return
        end = blob.rfind(b"\n")
        if end < 0:
            return
        self._offsets[path] = offset + end + 1
        for line in blob[: end + 1].splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.warning("skipping unparseable line in %s: %r", path, line[:120])
                continue
            if isinstance(record, dict):
                yield record
            else:
                log.warning("skipping non-object line in %s: %r", path, line[:120])

    def _action_from_push(self, record: dict[str, Any]) -> UserAction | None:
        """One forwarded callback record -> UserAction, or None (duplicate
        seq, or a callback outside the docs/27 §5 mapping)."""
        seq = record.get("seq")
        if isinstance(seq, int) and not self._remember_seq(seq):
            return None
        name: str | None = None
        kind: UserActionKind | None = None
        raw_kind = record.get("kind")
        fpd_id = record.get("id")
        raw_name = record.get("name")
        if isinstance(raw_kind, str) and raw_kind in _PUSH_KINDS:
            # A pre-mapped line (the producer already speaks §5 kinds).
            kind = raw_kind  # type: ignore[assignment]  # membership-checked above
            name = raw_name if isinstance(raw_name, str) else None
        elif isinstance(fpd_id, int) and fpd_id in _PUSH_BY_ID:
            name, kind = _PUSH_BY_ID[fpd_id]
        elif isinstance(raw_name, str) and raw_name in _PUSH_BY_NAME:
            name, kind = raw_name, _PUSH_BY_NAME[raw_name]
        if kind is None:
            log.debug("ignoring unmapped bridge event: %r", record)
            return None
        detail = {key: record[key] for key in ("index", "value", "seq", "ts_ns") if key in record}
        if name is not None:
            detail["callback"] = name
        return UserAction(kind=kind, at=self._clock(), detail=detail)

    def _remember_seq(self, seq: int) -> bool:
        """False for a seq already seen (the same event read from a second
        root, or re-read after a partial failure); bounded memory."""
        if seq in self._seen_seqs:
            return False
        if len(self._seen_ring) == self._seen_ring.maxlen:
            self._seen_seqs.discard(self._seen_ring[0])
        self._seen_ring.append(seq)
        self._seen_seqs.add(seq)
        return True

    # -- score edits (lazy poll-and-diff at checkpoints) -------------------------

    def poll(self) -> list[UserAction]:
        """The checkpoint read: ``score_edit`` events for every target whose
        saved notes changed since the last look. Costs one stat while the
        project file is unchanged; one parse when it is not (VERDICT Q4's
        cheap-poll verdict, made lazy). The FIRST poll seeds the baseline
        and emits nothing - pre-existing notes are not user actions."""
        if self._project is None:
            return []
        with self._lock:
            try:
                mtime_ns = self._project.stat().st_mtime_ns
            except OSError:
                return []
            if self._baseline is not None and mtime_ns == self._baseline_mtime_ns:
                return []
            score = self._read_score()
            if score is None:
                return []
            if self._baseline is None:
                self._baseline, self._baseline_mtime_ns = score, mtime_ns
                return []
            at = self._clock()
            actions: list[UserAction] = []
            for pattern, channel in sorted(set(self._baseline) | set(score)):
                delta = diff_notes(
                    self._baseline.get((pattern, channel), []), score.get((pattern, channel), [])
                )
                if delta.is_empty():
                    continue
                actions.append(
                    UserAction(
                        kind="score_edit",
                        at=at,
                        detail={"pattern": pattern, "channel": channel, "delta": delta},
                    )
                )
            self._baseline, self._baseline_mtime_ns = score, mtime_ns
            self._recent_edits.extend(actions)
            return actions

    def _read_score(self) -> dict[tuple[int, int], list[Note]] | None:
        """Every addressable target's notes from the project file, in tool
        units, or None for a file we refuse to diff (unparseable mid-save
        states resolve on the next checkpoint; a wrong diff would not)."""
        assert self._project is not None  # poll() gates on this
        try:
            project = flp.read(self._project)
            ppq = project.ppq or 96
            score: dict[tuple[int, int], list[Note]] = {}
            for n in project.notes:
                if n.pattern < 1:
                    continue  # unattributed blob; not addressable as a Target
                score.setdefault((n.pattern, n.channel), []).append(
                    Note(
                        key=n.key,
                        start=n.position / ppq,
                        length=n.length / ppq,
                        velocity=n.velocity / 127,
                        pan=n.pan / 128,
                    )
                )
        except (flp.FlpError, OSError, ValidationError):
            log.warning("could not read %s for a checkpoint diff; skipping it", self._project)
            return None
        return score

    # -- the concurrency rule (docs/27 §5) --------------------------------------

    def guard_write(self, target: Target, *, window: float = GUARD_WINDOW_SECONDS) -> None:
        """The gate a note lane calls before writing: runs the checkpoint
        poll, then raises ``UserEditing`` (a retryable ToolError) if a user
        ``score_edit`` touched ``target`` within ``window`` seconds. Returns
        None when the write may proceed."""
        self.poll()
        with self._lock:
            horizon = self._clock() - timedelta(seconds=window)
            for action in reversed(self._recent_edits):  # newest first
                if action.at < horizon:
                    break  # append-ordered, so everything earlier is staler
                if (
                    action.detail["pattern"] == target.pattern
                    and action.detail["channel"] == target.channel
                ):
                    raise UserEditing(target, action, window)

    def record_agent_write(self, target: Target, notes: list[Note]) -> None:
        """The agent-echo suppressant: after a verified write, the note lane
        reports the target's post-write notes (its READBACK, never its
        intent) so the next checkpoint does not attribute the agent's own
        change to the user - and so the guard never blocks the agent on
        itself."""
        with self._lock:
            if self._baseline is not None:
                self._baseline[(target.pattern, target.channel)] = list(notes)
