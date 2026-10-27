"""The tool error model (docs/06 "Error model").

Every tool-facing failure is structured and names the fix, so the model can
act on it instead of retrying blindly:

    { "error": "note_plane_unavailable",
      "message": "FL Studio did not take the note edit within 10s.",
      "fix": "FL may be busy. Retry the write...",
      "retryable": true }

``message`` and ``fix`` are read by the model, so they speak about FL Studio,
tracks and the song - never about the engine's internals (the Bridge, lanes,
spools, the project file); those details go to the log. A test scans every
error string for that vocabulary.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

ErrorCategory = Literal[
    "concurrent_write",
    "control_plane_unavailable",
    "control_plane_busy",
    "note_plane_unavailable",
    "invalid_argument",
    "internal_error",
    "unknown_target",
    "fl_api_error",
    "kernel_unavailable",
    "unsupported_in_live_session",
    "render_failed",
    "stale_handle",
    "operation_unsafe",
    "fl_relaunch_required",
    "file_lane_requires_reload",
    "fl_trial_unsupported",
    "unsupported_fl_build",
]


class ToolError(Exception):
    """A tool failure with everything the model needs to react correctly."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        fix: str,
        *,
        retryable: bool = False,
        candidates: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.fix = fix
        self.retryable = retryable
        self.candidates = candidates

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "error",
            "error": self.category,
            "message": self.message,
            "fix": self.fix,
            "retryable": self.retryable,
        }
        if self.candidates is not None:
            payload["candidates"] = self.candidates
        return payload


# The default fix when FL cannot take an edit at all (setup is the operator's
# job; fl-mcp-doctor and the log name the missing piece).
_SETUP_FIX = "FL Studio may still be starting. Wait a moment and retry."


class NotePlaneUnavailable(ToolError):
    """FL cannot take this change now; by default that is transient (FL starting)."""

    def __init__(self, message: str, fix: str = _SETUP_FIX, *, retryable: bool = True) -> None:
        super().__init__("note_plane_unavailable", message, fix, retryable=retryable)


class WriteRejected(ToolError):
    """A backend took the call but did not CONFIRM the requested state."""

    def __init__(self, message: str, fix: str, *, retryable: bool = True) -> None:
        super().__init__("fl_api_error", message, fix, retryable=retryable)


class ConcurrentWrite(ToolError):
    """A write would race another write already in progress."""

    def __init__(self, message: str, fix: str, *, retryable: bool = True) -> None:
        super().__init__("concurrent_write", message, fix, retryable=retryable)


def note_plane_unavailable(command_id: int, timeout: float) -> ToolError:
    return ToolError(
        "note_plane_unavailable",
        f"FL Studio did not take note edit {command_id} within {timeout:g}s.",
        "FL may be busy (a dialog or a long operation). Retry the write, preferably with "
        "mode='replace' so an edit that lands late cannot duplicate notes.",
        retryable=True,
    )


def control_plane_unavailable(op: str, timeout: float) -> ToolError:
    log.info("FL did not answer %r within %gs", op, timeout)
    return ToolError(
        "control_plane_unavailable",
        "FL Studio did not answer in time.",
        "FL may be busy or still starting. Retry in a moment.",
        retryable=True,
    )


def control_plane_busy(op: str, timeout: float) -> ToolError:
    """The shared wire was held by another session's op - contended, NOT dead.

    Distinct from control_plane_unavailable so liveness checks treat a busy
    wire as live (the op just needs a retry), rather than declaring FL gone.
    session.link.request retries it before a caller sees it."""
    log.info("the control wire stayed held for %gs; %r was not sent", timeout, op)
    return ToolError(
        "control_plane_busy",
        "FL Studio stayed busy and did not take this action, so nothing changed.",
        "Retry it in a few seconds.",
        retryable=True,
    )


def concurrency_guard_unavailable(project: Path, detail: str) -> ToolError:
    log.warning("cannot check %s for concurrent user edits: %s", project, detail)
    return ToolError(
        "operation_unsafe",
        "Could not check whether someone is editing this track in FL right now, so the "
        "write was not made (it could overwrite their edit).",
        "Retry the write in a moment.",
        retryable=True,
    )


def concurrent_write(action: str) -> ToolError:
    return ConcurrentWrite(
        f"Another edit is still being applied, so {action} was not started.",
        "Retry when it finishes.",
    )


def concurrent_write_wait_expired(action: str, remaining_action: str, retry_seconds: float) -> ToolError:
    """A render's bounded wait for the active write elapsed."""
    return ConcurrentWrite(
        f"{action} waited {retry_seconds:g}s for the edit '{remaining_action}', "
        "which is still being applied.",
        f"Retry in {retry_seconds:g} seconds after '{remaining_action}' finishes.",
    )


def unknown_target(kind: str, name: str | int, candidates: list[str]) -> ToolError:
    listed = ", ".join(candidates[:8]) + ("…" if len(candidates) > 8 else "")
    return ToolError(
        "unknown_target",
        f"No {kind} {name!r}.",
        f"Available: {listed or '(none)'}. "
        f"{kind.capitalize()}s are fixed by the template - pick one or ask the "
        "user to add it.",
        candidates=candidates,
    )


def fl_api_error(op: str, detail: str) -> ToolError:
    return ToolError(
        "fl_api_error",
        f"'{op}' failed inside FL Studio: {detail}",
        "This is FL Studio's own error message. Check the arguments (track name or index) and retry.",
    )


def invalid_argument(call: str, detail: str) -> ToolError:
    return ToolError(
        "invalid_argument",
        f"{call} received invalid arguments: {detail}",
        "Fix the argument type or range before retrying.",
    )


def kernel_unavailable(detail: str) -> ToolError:
    """The run kernel could not start, so no model code ran."""
    return ToolError(
        "kernel_unavailable",
        f"The Python kernel for run could not start: {detail}",
        "This is an engine setup problem on this host, not your code. FL and the project are "
        "unaffected; the other tools still work. Retry once, then report it.",
        retryable=True,
    )


def workspace_unavailable(detail: str, *, retryable: bool = True) -> ToolError:
    """The confined process for a workspace tool could not start or failed its boundary check.

    The category is the run kernel's: the same launcher and OS boundary failed.
    """
    return ToolError(
        "kernel_unavailable",
        f"The workspace tools could not start: {detail}",
        "This is an engine setup problem on this host, not your command; your files are unaffected. "
        + ("Retry once, then report it." if retryable else "Report it; retrying will not help."),
        retryable=retryable,
    )


_UNSAFE_MARKER = "operation unsafe at current time"


def is_operation_unsafe(detail: str) -> bool:
    """Whether an FL error string is the mid-cycle scripting-thread-safety guard
    (docs/14), rather than a real API failure - worth a plain retry, not a bug report."""
    return _UNSAFE_MARKER in detail.lower()


def operation_unsafe(op: str) -> ToolError:
    """FL kept refusing a call as unsafe for the whole bounded retry
    (session.link.request), so nothing changed. The op goes to the log."""
    log.warning("FL refused %r as unsafe at the current time through every retry", op)
    return ToolError(
        "operation_unsafe",
        "FL Studio stayed busy for several seconds and did not take this action, so nothing changed.",
        "Retry it in a few seconds.",
        retryable=True,
    )


def fl_trial_unsupported(action: str) -> ToolError:
    """A trial FL cannot reopen a saved project, and answers Save with a modal
    Buy dialog that freezes the controller (FLMCP_FL_LICENSE=trial)."""
    return ToolError(
        "fl_trial_unsupported",
        f"This FL Studio is a trial, which cannot save or reopen songs, so {action} is not available.",
        "Nothing is lost: your edits are live in FL and render plays them. Notes, tempo, "
        "parameters and levels all work; saving, automation clips, adding effects and offline "
        "export do not.",
        retryable=False,
    )


def fl_not_prepared(category: ErrorCategory, consequence: str) -> ToolError:
    """No Bridge is running in FL: notes and render need it, and only the agent
    project carries it. A model hears that as "the prepared song is not open";
    fl-mcp-doctor names the exact cause (not installed, another song open, FL
    still loading) for the user."""
    return ToolError(
        category,
        f"FL Studio is not ready for you, so {consequence}: the song prepared for you "
        "is not open in FL, or FL is still opening it.",
        "If FL was just opened, retry in a moment. If it keeps failing, ask the user to open "
        "the FLMCP Agent song in FL Studio (File > Open, in FL's Projects folder); if it is "
        "already open, the user runs fl-mcp-doctor, which names what is missing.",
        retryable=True,
    )


def file_lane_requires_reload() -> ToolError:
    """Session mode has no safe in-place project reload primitive."""
    return ToolError(
        "file_lane_requires_reload",
        "This edit rewrites the saved song, which FL would have to reopen; FL is never reopened during a match.",
        "Notes, tempo, parameters and levels work live. Automation clips and adding effects are "
        "not available during a match.",
        retryable=False,
    )
