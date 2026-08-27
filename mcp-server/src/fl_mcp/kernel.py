"""The notebook-session kernel surface (docs/11 "The Python notebook session",
"B. Kernel surface") — what the model's Python code in the notebook talks to:

    fl = kernel.bootstrap(session)
    bass = fl.track("Bass")             # name resolved to a channel once, cached
    bass.notes()                        # lazy handle + summary, not a full dump
    bass.write(chord("C4"), mode="merge")
    fl.flush()                          # or: with fl.cell(): ...

Built entirely on session.py's control/records/actions primitives; nothing
here changes schemas.py — see KERNEL_FINDINGS.md for the seam decisions this
made (consuming Layer 1 events as loose dicts, deferring bootstrap's
ground-truth note read to M4's sync_from_project).

Control ops route through ``session.control`` — in production that's
``control_midi.get_control()`` (docs/14's live-verified MIDI transport); every
op name below (``transport.*``, ``mixer.*``, ``channels.*``, ``general.*``,
``state.*``) is one the controller script's dispatch table actually serves
(fl-scripts/controller/device_FLMCP.py), not the earlier draft names.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel

from . import errors, schemas
from .notes import describe, parse_key
from .session import FLSession
from .spool import write_json_atomic

# Structure-only signals (docs/11 Layer 1): note edits never fire these, so
# they invalidate cached channel/pattern numbers and the manifest, not the
# shadow score.
_STRUCTURAL_EVENTS = {"dirty_channel", "dirty_mixer_track", "refresh"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check(resp: schemas.ControlResponse, op: str) -> schemas.ControlResponse:
    """Raise a structured error for a failed control op; pass through on success.

    FL's own "Operation unsafe at current time" guard (docs/14: some mutating
    ops reject calls made mid-engine-cycle) is named explicitly as retryable
    rather than folded into the generic fl_api_error — the model should just
    retry, not treat it as a real API failure.
    """
    if not resp.ok:
        detail = resp.err or "unknown error"
        if errors.is_operation_unsafe(detail):
            raise errors.operation_unsafe(op)
        raise errors.fl_api_error(op, detail)
    return resp


# ---------------------------------------------------------------------------
# Manifest — the bootstrap's "ask FL for the layout" result, persisted to disk
# ---------------------------------------------------------------------------


class ChannelInfo(BaseModel):
    idx: int
    name: str


class MixerTrackInfo(BaseModel):
    idx: int
    name: str


class Manifest(BaseModel):
    schema_version: int = 1
    generated_at: datetime
    ppq: int
    pattern_count: int
    channels: list[ChannelInfo]
    mixer: list[MixerTrackInfo]


def _manifest_from_snapshot(snap: dict[str, Any]) -> Manifest:
    return Manifest(
        generated_at=_now(),
        ppq=snap["ppq"],
        pattern_count=snap["pattern_count"],
        channels=[ChannelInfo(idx=c["idx"], name=c["name"]) for c in snap["channels"]],
        mixer=[MixerTrackInfo(idx=t["idx"], name=t["name"]) for t in snap["mixer"]],
    )


def _manifest_path(session: FLSession):
    return session.shadow.root / "manifest.json"


def _save_manifest(session: FLSession, manifest: Manifest) -> None:
    write_json_atomic(_manifest_path(session), manifest.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Handles
# ---------------------------------------------------------------------------


@dataclass
class NoteSetHandle:
    """bass.notes() result: a summary the model can read without a full dump,
    plus the underlying notes one call away.

    "Lazy" here means the model isn't handed the full note list unless it
    asks — the shadow-store lookup behind this is already an in-memory dict
    read (docs/11 Layer 2: model-written content is exact and free), so there
    is no deferred I/O to hide; ``materialize()`` exists for the model's
    context budget, not for performance.
    """

    target: schemas.Target
    ref: schemas.NoteSetRef | None
    summary: str
    _notes: list[schemas.Note]

    def materialize(self) -> list[schemas.Note]:
        return list(self._notes)

    def __len__(self) -> int:
        return len(self._notes)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        stale = " (stale)" if self.ref and self.ref.stale else ""
        return f"<NoteSetHandle {self.target.pattern}:{self.target.channel} {self.summary}{stale}>"


@dataclass
class _QueuedWrite:
    generation: int
    track_name: str
    target: schemas.Target
    notes: list[schemas.Note]
    mode: str


@dataclass
class _QueuedClear:
    generation: int
    track_name: str
    target: schemas.Target


class Track:
    """fl.track("Bass") result: a cached name->channel handle.

    Guarded by ``ref.generation`` — every method refuses to run once the
    kernel's generation has moved past the one this handle was resolved at
    (docs/11: "if the project was reloaded or undone since the last cell, the
    session detects it and refuses to act on stale handles").
    """

    def __init__(self, kernel: "FLKernel", ref: schemas.TrackRef) -> None:
        self._kernel = kernel
        self.ref = ref

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def channel(self) -> int:
        return self.ref.channel

    def _check_fresh(self) -> None:
        if self.ref.generation != self._kernel.generation:
            raise errors.stale_handle("track", self.ref.name)

    def _target(self, pattern: int | str) -> schemas.Target:
        return schemas.Target(pattern=pattern, channel=self.ref.channel)

    def notes(self, filter: Callable[[schemas.Note], bool] | None = None, pattern: int | str = -1) -> NoteSetHandle:
        self._check_fresh()
        target = self._target(pattern)
        ref = self._kernel.session.shadow.ref_at(target)
        stored = self._kernel.session.shadow.notes_at(target) or []
        # The shadow store keeps notes exactly as written (key may still be a
        # name like "C4"); resolve to MIDI ints here so the model's filter
        # predicate and the materialized notes are consistently comparable
        # regardless of how each note was originally spelled.
        notes = [n if isinstance(n.key, int) else n.model_copy(update={"key": parse_key(n.key)}) for n in stored]
        if filter is not None:
            notes = [n for n in notes if filter(n)]
        summary = describe([n.canonical() for n in notes])
        return NoteSetHandle(target=target, ref=ref, summary=summary, _notes=notes)

    def write(self, notes: list[schemas.Note], mode: str = "merge", pattern: int | str = -1) -> None:
        """Queue a write; it applies when the cell flushes (``fl.flush()`` / ``fl.cell()``)."""
        self._check_fresh()
        if mode not in ("merge", "replace"):
            raise ValueError(f"mode must be 'merge' or 'replace', got {mode!r}")
        self._kernel._queue.append(
            _QueuedWrite(
                generation=self._kernel.generation,
                track_name=self.ref.name,
                target=self._target(pattern),
                notes=list(notes),
                mode=mode,
            )
        )

    def clear(self, pattern: int | str = -1) -> None:
        self._check_fresh()
        self._kernel._queue.append(
            _QueuedClear(generation=self._kernel.generation, track_name=self.ref.name, target=self._target(pattern))
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Track {self.ref.name!r} channel={self.ref.channel}>"


# ---------------------------------------------------------------------------
# FLKernel — the "fl" object
# ---------------------------------------------------------------------------


class FLKernel:
    """One notebook session's kernel, bound to one FLSession.

    Construct via :func:`bootstrap` for an existing project; the bare
    constructor is also safe (a fresh/empty project self-heals its manifest
    the first time ``track()`` needs it — no separate code path to keep in
    sync with bootstrap's).
    """

    def __init__(self, session: FLSession) -> None:
        self.session = session
        self.generation = 0
        self.manifest: Manifest | None = None
        self._tracks_by_name: dict[str, Track] = {}
        self._queue: list[_QueuedWrite | _QueuedClear] = []
        if session.control is not None:
            session.control.add_event_listener(self._on_control_event)

    # -- staleness / structure -------------------------------------------------

    def _on_control_event(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        if op == "project_load":
            self.generation += 1
            self.manifest = None
            self._tracks_by_name.clear()
            self.session.shadow.mark_stale()
        elif op in _STRUCTURAL_EVENTS:
            self.generation += 1
            self.manifest = None
            self._tracks_by_name.clear()

    def _require_control(self, op: str):
        if self.session.control is None:
            raise errors.control_plane_unavailable(op, 0.0)
        return self.session.control

    def _fetch_manifest(self) -> Manifest:
        resp = _check(self._require_control("state.snapshot").send_request("state.snapshot"), "state.snapshot")
        manifest = _manifest_from_snapshot(resp.ret)
        _save_manifest(self.session, manifest)
        return manifest

    def _ensure_manifest(self) -> Manifest:
        if self.manifest is None:
            self.manifest = self._fetch_manifest()
        return self.manifest

    # -- track resolution -------------------------------------------------------

    def track(self, name: str | int) -> Track:
        """Resolve a channel by name (cached) or number; returns a live handle."""
        cached = self._tracks_by_name.get(name) if isinstance(name, str) else None
        if cached is not None and cached.ref.generation == self.generation:
            return cached

        manifest = self._ensure_manifest()
        if isinstance(name, int):
            channel_idx = name
            match = next((c for c in manifest.channels if c.idx == channel_idx), None)
            channel_name = match.name if match is not None else str(channel_idx)
        else:
            match = next((c for c in manifest.channels if c.name == name), None)
            if match is None:
                raise errors.unknown_target("channel", name, [c.name for c in manifest.channels])
            channel_idx = match.idx
            channel_name = match.name

        handle = Track(self, schemas.TrackRef(channel=channel_idx, name=channel_name, generation=self.generation))
        if isinstance(name, str):
            self._tracks_by_name[name] = handle
        return handle

    # -- write queue / cells -----------------------------------------------------

    def flush(self) -> list[dict[str, Any]]:
        """Apply every queued write/clear, in order. Stops (leaving the rest
        queued) if a handle went stale mid-queue or the note plane rejects one."""
        results = []
        while self._queue:
            item = self._queue[0]
            if item.generation != self.generation:
                raise errors.stale_handle("track", item.track_name)
            self._queue.pop(0)
            if isinstance(item, _QueuedWrite):
                resp, _ = self.session.write_notes(item.target, item.notes, item.mode)
                results.append(
                    {
                        "op": "add_notes",
                        "track": item.track_name,
                        "ok": resp.ok,
                        "notes_written": resp.notes_written,
                        "err": resp.err,
                    }
                )
            else:
                resp, _cmd = self.session.clear_notes(item.target)
                results.append(
                    {"op": "clear_notes", "track": item.track_name, "ok": resp.ok, "err": resp.err}
                )
        return results

    @contextmanager
    def cell(self):
        """``with fl.cell(): ...`` — flushes queued writes at the end, unless
        the block raised (an errored cell shouldn't apply partial work)."""
        yield self
        self.flush()

    # -- transport ----------------------------------------------------------

    def play(self) -> None:
        _check(self._require_control("transport.start").send_request("transport.start"), "transport.start")

    def stop(self) -> None:
        _check(self._require_control("transport.stop").send_request("transport.stop"), "transport.stop")

    def set_tempo(self, bpm: float) -> None:
        _check(
            self._require_control("mixer.setCurrentTempo").send_request("mixer.setCurrentTempo", args=[bpm]),
            "mixer.setCurrentTempo",
        )

    def undo_last(self) -> schemas.ActionEntry:
        """Undo the model's most recent recorded action, in FL and in our records."""
        last = self.session.actions.last()
        if last is None:
            raise errors.ToolError(
                "fl_api_error", "Nothing recorded to undo.", "No actions have been logged for this project yet."
            )
        _check(self._require_control("general.undo").send_request("general.undo"), "general.undo")
        if last.target is not None:
            self.session.shadow.mark_stale(last.target)
        # Conservative: we don't know exactly what the undo touched, so treat
        # it like a reload for handle purposes (docs/11: undo must invalidate
        # stale handles same as a reload does).
        self.generation += 1
        self.manifest = None
        self._tracks_by_name.clear()
        return last


# ---------------------------------------------------------------------------
# Bootstrap (docs/11 "Starting from an existing project")
# ---------------------------------------------------------------------------


def bootstrap(session: FLSession, project_reader: Callable[[FLSession], None] | None = None) -> FLKernel:
    """Ask FL for the layout, save it, open the action log with a "bootstrap"
    entry, and return a ready :class:`FLKernel`.

    ``project_reader`` is a pluggable ground-truth note read (docs/11 step 2:
    "save a copy of the project and read the notes out of it") — left as an
    injectable seam because that read depends on ``sync_from_project`` (PyFLP),
    which is M4 scope and not implemented yet anywhere in this codebase. With
    no reader supplied, bootstrap still does the structural half (channels,
    patterns, mixer, names) and records that the note read is pending.
    """
    kernel = FLKernel(session)
    kernel.manifest = kernel._fetch_manifest()

    ground_truth = "pending: sync_from_project (PyFLP ground-truth read) is M4 scope, not implemented yet"
    if project_reader is not None:
        project_reader(session)
        ground_truth = "read"

    session.actions.append(
        schemas.ActionEntry(
            ts=_now(),
            id=0,
            gen=0,
            op="bootstrap",
            after={
                "channels": len(kernel.manifest.channels),
                "mixer_tracks": len(kernel.manifest.mixer),
                "pattern_count": kernel.manifest.pattern_count,
                "ground_truth_notes": ground_truth,
            },
        )
    )
    return kernel
