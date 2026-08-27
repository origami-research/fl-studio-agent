"""Backend router: one place that picks how an edit lands, and records it.

docs/17's load-bearing piece. Note-writing has several mechanisms (live
in-place, project-write rewrite+reopen, queued/manual, and the Bridge plugin
for explicit targets); the router tries them live-first and, whichever one
applies, records the *same* structured change into the memory layer - the
shadow score and the action log's foldable NoteDelta history. The layers above
(compression, history, kernel) are therefore backend-agnostic: they see one
delta per write regardless of which mechanism moved the notes.

This wraps the existing cascade rather than rewriting it, so every backend's
user-facing return shape is preserved exactly; the router only adds the
write-through as a side effect. The edit lands at the backend first, so a
recording failure must not fail the tool - but it is logged with a traceback,
never silently dropped, so a real bug in the recording path surfaces.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from . import diff, errors, schemas
from .actions import ActionLog
from .records import ShadowStore

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_note_write(
    shadow: ShadowStore,
    actions: ActionLog,
    target: schemas.Target,
    parsed_notes: list[dict[str, Any]],
    mode: str,
    backend: str,
    command_id: int,
    gen: int = 0,
) -> schemas.NoteDelta | None:
    """Write-through the memory layer for a note write that already landed.

    Backend-agnostic: given the notes and mode that were applied, it updates the
    shadow score and appends an ActionEntry carrying the before/after hashes and
    the structured NoteDelta. Returns the delta, or None if recording failed
    (never raises - the edit itself already succeeded).
    """
    mode_lit: Literal["merge", "replace"] = "replace" if mode == "replace" else "merge"
    try:
        before_ref = shadow.ref_at(target)
        before_notes = shadow.notes_at(target) or []
        typed = [schemas.Note.model_validate(n) for n in parsed_notes]
        ref = shadow.write(target, typed, mode=mode_lit)
        after_notes = shadow.notes_by_id(ref.id) or []
        delta = diff.diff_notes(before_notes, after_notes)
        actions.append(
            schemas.ActionEntry(
                ts=_now(),
                id=command_id,
                gen=gen,
                op="add_notes",
                target=target,
                mode=mode_lit,
                before_hash=before_ref.id if before_ref else None,
                after_hash=ref.id,
                delta=delta,
                undo_step=f"{backend}: write notes ({target.pattern}:{target.channel})",
            )
        )
    except Exception:
        # The edit already landed at the backend; a recording failure must not
        # fail the tool. But it must not be SILENT either - log it with a
        # traceback so a real bug here surfaces instead of vanishing.
        log.exception("write-through recording failed after the %s backend applied the edit", backend)
        return None
    else:
        return delta


# A note backend: given (parsed notes, mode, channel, command_id) it either
# applies the write and returns its user-facing result dict, or returns None to
# fall through to the next backend. The registry is ordered live-first.
NoteBackend = Callable[[list[dict[str, Any]], str, "str | int | None", int], "dict[str, Any] | None"]


def try_live_control(
    session: Any,
    op: str,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> Any | None:
    """Try one op over the live MIDI control plane; None if unavailable/failed.

    The live tier of the control cascade (docs/17): a connected control plane
    edits FL in place with no reopen. Returns the ControlResponse on success,
    or None so the caller falls back to the file backend (project-write). Never
    raises - a missing/timed-out control plane is a fall-through, not an error.
    FL's "unsafe at current time" guard is handled controller-side (the OnIdle
    deferral), so a transient engine-busy doesn't surface here.
    """
    if session.control is None:
        return None
    try:
        resp = session.control.send_request(op, args=args, kwargs=kwargs, timeout=timeout)
    except errors.ToolError:
        return None  # control plane didn't answer; fall back to the file backend
    return resp if resp.ok else None


def route_note_write(
    backends: list[tuple[str, NoteBackend]],
    parsed_notes: list[dict[str, Any]],
    mode: str,
    channel: str | int | None,
    command_id: int,
    on_applied: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """Try each backend in order; return the first non-None result.

    ``on_applied(backend_name, result)`` fires for the backend that applied, so
    the caller can record the write-through with the target it actually hit.
    Returns None if no backend applied (caller falls back to queueing).
    """
    for name, backend in backends:
        result = backend(parsed_notes, mode, channel, command_id)
        if result is not None:
            if on_applied is not None:
                on_applied(name, result)
            return result
    return None
