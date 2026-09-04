"""The tool error model (docs/06 "Error model").

Every tool-facing failure is structured and names the fix, so the model can
act on it instead of retrying blindly:

    { "error": "note_plane_unavailable",
      "message": "The FL MCP Bridge plugin didn't pick up the command within 2s.",
      "fix": "In FL Studio: open the mixer (F9)...",
      "retryable": true }
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

ErrorCategory = Literal[
    "concurrent_write",
    "control_plane_unavailable",
    "control_plane_busy",
    "note_plane_unavailable",
    "invalid_argument",
    "internal_error",
    "unknown_target",
    "fl_api_error",
    "restricted_namespace",
    "unsupported_in_live_session",
    "render_failed",
    "stale_handle",
    "operation_unsafe",
    "fl_relaunch_required",
    "file_lane_requires_reload",
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


# The default fix when no note backend exists. The pyscript name matches
# install.SCRIPT_FILENAME (a frozen FL-side surface, duplicated like spool.py's
# copy because errors must stay a leaf module).
_SETUP_FIX = (
    "Either launch FL Studio with the FLMCP controller script configured (edits then "
    "apply via the 'MCP Write Notes' piano-roll script), or "
    "configure a project file the agent may rewrite: run "
    "`python -m fl_mcp.make_agent_project`, or set FLMCP_AGENT_PROJECT to a .flp path."
)


class NotePlaneUnavailable(ToolError):
    """No backend can apply this change; the fix names exactly what to set up."""

    def __init__(self, message: str, fix: str = _SETUP_FIX) -> None:
        super().__init__("note_plane_unavailable", message, fix)


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
        f"The FL MCP Bridge plugin didn't pick up command {command_id} within {timeout:g}s.",
        "In FL Studio: open the mixer (F9), select the 'MCP' insert, and confirm "
        "'FL MCP Bridge' is loaded. Run fl_doctor after.",
        retryable=True,
    )


def control_plane_unavailable(op: str, timeout: float) -> ToolError:
    return ToolError(
        "control_plane_unavailable",
        f"No response to '{op}' from the controller script within {timeout:g}s.",
        "In FL Studio: open MIDI settings and confirm 'FLMCP Req'/'FLMCP Res' are "
        "assigned to the FLMCP controller script. Run fl_doctor after.",
        retryable=True,
    )


def control_plane_busy(op: str, timeout: float) -> ToolError:
    """The shared wire was held by another session's op - contended, NOT dead.

    Distinct from control_plane_unavailable so liveness checks treat a busy
    wire as live (the op just needs a retry), rather than declaring FL gone."""
    return ToolError(
        "control_plane_busy",
        f"The control wire was busy for {timeout:g}s and did not free for '{op}'.",
        "Another session's control op held the shared wire; retry.",
        retryable=True,
    )


def concurrency_guard_unavailable(project: Path, detail: str) -> ToolError:
    return ToolError(
        "operation_unsafe",
        f"Cannot verify whether the user has unsaved score edits in {project.name}: {detail}.",
        "Retry after FL finishes saving, or run fl_doctor and restore access to the project file. "
        "The write is refused because clobbering a user's concurrent edit would be unsafe.",
        retryable=True,
    )


def concurrent_write(action: str) -> ToolError:
    return ConcurrentWrite(
        f"{action} refused because another FL MCP write is already in progress.",
        "Wait for the in-flight write and FL reload/readback to finish, then retry.",
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
        "This is FL's own error, verbatim from the controller script. Check the "
        "arguments (target name/index) and retry.",
    )


def invalid_argument(call: str, detail: str) -> ToolError:
    return ToolError(
        "invalid_argument",
        f"{call} received invalid arguments: {detail}",
        "Fix the argument type or range before retrying.",
    )


def restricted_namespace(offender: str) -> ToolError:
    """Code outside run's music-only namespace is refused before execution."""
    return ToolError(
        "restricted_namespace",
        f"{offender} is unavailable in run's music-only namespace; use this tool's SDK cheatsheet for supported music code.",
        "Use fl, theory, Note, or an allowlisted standard-library module.",
    )


_UNSAFE_MARKER = "operation unsafe at current time"


def is_operation_unsafe(detail: str) -> bool:
    """Whether an FL error string is the mid-cycle scripting-thread-safety guard
    (docs/14), rather than a real API failure - worth a plain retry, not a bug report."""
    return _UNSAFE_MARKER in detail.lower()


def operation_unsafe(op: str) -> ToolError:
    return ToolError(
        "operation_unsafe",
        f"FL Studio refused '{op}': operation unsafe at current time.",
        "FL's own scripting-thread-safety guard - it rejects some mutating calls made "
        "mid-engine-cycle. Retrying (optionally after a short delay) usually succeeds.",
        retryable=True,
    )


def stale_handle(kind: str, name: str | int) -> ToolError:
    return ToolError(
        "stale_handle",
        f"This {kind} handle for {name!r} was resolved before the project layout changed "
        "(reload, undo, or a structure signal from FL) and can no longer be trusted.",
        f"Get a fresh handle - call fl.track({name!r}) again - before retrying this cell.",
        retryable=True,
    )


def unsupported_in_live_session(action: str) -> ToolError:
    return ToolError(
        "unsupported_in_live_session",
        f"{action} is not possible while FL Studio is running with a live session.",
        "This requires the template project convention (docs/07) - set it up at "
        "project-creation time, not during a live session.",
    )


def file_lane_requires_reload() -> ToolError:
    """Session mode has no safe in-place project reload primitive."""
    return ToolError(
        "file_lane_requires_reload",
        "This edit requires reopening the project file, which is unavailable during a live session.",
        "Use live lanes during the match: notes through the Bridge; tempo, plugin parameters, and "
        "channel levels through the control wire. Automation clips and adding effects have no live "
        "equivalent yet. Retry after the session ends.",
        retryable=True,
    )
