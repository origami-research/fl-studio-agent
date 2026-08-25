"""The autonomous backend: write notes into the project, then show them in FL.

No user action, no macOS permission, no hotkey. It works because both halves are
unprivileged: editing a `.flp` is just writing a file, and `open -a` is the
documented way to open a document — neither needs automation consent.

The trade-off is honest and worth stating: FL *reloads* the project rather than
being edited live. Anything unsaved in the running session is lost, so this
backend is for agent-driven composition in a dedicated project, not for editing
over someone's shoulder while they work.

Selection logic lives in `available()`: this is used only when an agent project
is configured, so the default remains the non-destructive spool path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import flpaths

# Set FLMCP_AGENT_PROJECT to point at the project the agent may rewrite.
ENV_PROJECT = "FLMCP_AGENT_PROJECT"
DEFAULT_PROJECT_NAME = "FLMCP Agent.flp"


def agent_project() -> Path | None:
    """The project this backend is allowed to modify, if configured."""
    override = os.environ.get(ENV_PROJECT)
    if override:
        return Path(override).expanduser()

    settings = flpaths.primary_settings_dir()
    if settings is None:
        return None
    candidate = settings.parent / "Projects" / DEFAULT_PROJECT_NAME
    return candidate if candidate.is_file() else None


def available() -> bool:
    return agent_project() is not None


# Names that mean "this channel is one drum sample", where a melody would be
# both inaudible as written and invisible to anyone looking for it.
_PERCUSSIVE = (
    "drum", "kick", "snare", "clap", "hat", "hihat", "crash", "ride", "tom",
    "perc", "rim", "shaker", "cymbal", "tamb", "shake", "808", "fpc",
)


def list_channels() -> list[dict[str, Any]]:
    """Channels in the agent project, flagged melodic or percussive."""
    project = agent_project()
    if project is None:
        return []
    from harness.flp_compat import parse

    out: list[dict[str, Any]] = []
    for index, channel in enumerate(parse(project).channels):
        name = ""
        try:
            name = channel.name or ""
        except Exception:  # noqa: BLE001 - some channel types have no name
            pass
        lowered = name.lower()
        out.append(
            {
                "index": index,
                "name": name or f"Channel {index}",
                "melodic": not any(word in lowered for word in _PERCUSSIVE),
            }
        )
    return out


# Preference order when nothing is specified. A melody belongs on a lead before
# a bass, and on anything before a drum.
_ROLE_PREFERENCE = ("lead", "synth", "pluck", "chord", "key", "piano", "pad", "bass")


def default_channel() -> int:
    """Pick the most melody-appropriate channel.

    Rack 0 is a drum in most templates, so defaulting to it silently put
    melodies somewhere they could be neither heard properly nor found.
    """
    channels = list_channels()
    melodic = [c for c in channels if c["melodic"]]
    if not melodic:
        return 0
    for role in _ROLE_PREFERENCE:
        for channel in melodic:
            if role in channel["name"].lower():
                return channel["index"]
    return melodic[0]["index"]


class ChannelNotFound(ValueError):
    """Raised with the available names, so the caller can pick a real one."""


def resolve_channel(channel: int | str | None) -> int:
    """Accept an index, a channel name, or None for the best default.

    Names are matched case-insensitively, exact first then as a substring, so
    "lead" finds "Lead" and "Synth 1" is reachable as "synth 1" or "synth".
    """
    channels = list_channels()
    if channel is None:
        return default_channel()
    if isinstance(channel, int) and not isinstance(channel, bool):
        if any(c["index"] == channel for c in channels):
            return channel
        raise ChannelNotFound(
            f"No channel {channel}. Available: "
            + ", ".join(f"{c['index']}={c['name']}" for c in channels)
        )

    wanted = str(channel).strip().lower()
    for candidate in channels:
        if candidate["name"].lower() == wanted:
            return candidate["index"]
    matches = [c for c in channels if wanted in c["name"].lower()]
    if len(matches) == 1:
        return matches[0]["index"]
    if len(matches) > 1:
        raise ChannelNotFound(
            f"{channel!r} matches several channels: "
            + ", ".join(c["name"] for c in matches)
        )
    raise ChannelNotFound(
        f"No channel named {channel!r}. Available: "
        + ", ".join(c["name"] for c in channels)
    )


def fl_app() -> Path:
    return Path(os.environ.get("FL_APP", "/Applications/FL Studio 2025.app"))


def fl_is_running() -> bool:
    return subprocess.run(["pgrep", "-f", "FL Studio"], capture_output=True).returncode == 0


def _stop_fl(timeout: float = 20.0) -> None:
    """Kill FL so it reloads the file we just changed.

    SIGKILL, because a polite quit prompts to save and would hang unattended.
    Only ever applied to the agent's own project workflow, where the file on
    disk — not the in-memory session — is the source of truth.
    """
    subprocess.run(["pkill", "-9", "-f", "FL Studio"], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not fl_is_running():
            return
        time.sleep(0.2)


def _open_project(project: Path, attempts: int = 6) -> None:
    """Open the project in FL, retrying while LaunchServices settles.

    Right after a SIGKILL, `open` can fail with
    "procNotFound: no eligible process with specified descriptor" because
    LaunchServices still has the dead process registered. It clears in a second
    or two, so retry rather than surfacing a spurious failure.
    """
    last = ""
    for attempt in range(attempts):
        result = subprocess.run(
            ["open", "-a", str(fl_app()), str(project)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        last = (result.stderr or "").strip()
        time.sleep(1.0 + attempt)
    raise RuntimeError(f"could not open FL Studio after {attempts} attempts: {last}")


def apply_notes(
    notes: list[dict[str, Any]],
    mode: str = "merge",
    pattern: int = 1,
    channel: int | str | None = None,
    reload_fl: bool = True,
) -> dict[str, Any]:
    """Write notes into the agent project and show them in FL.

    `channel` accepts an index or a name ("Lead"); None picks the most
    melody-appropriate channel.
    """
    from .flp_writer import write_notes

    try:
        channel = resolve_channel(channel)
    except ChannelNotFound as exc:
        return {"ok": False, "error": str(exc)}

    project = agent_project()
    if project is None:
        return {
            "ok": False,
            "error": (
                "No agent project configured. Create one with "
                "`python -m fl_mcp.make_agent_project`, or set "
                f"{ENV_PROJECT} to a .flp the agent may rewrite."
            ),
        }

    # The project is rewritten in place; keep one rollback copy so a bad write
    # never destroys work outright.
    backup = project.with_suffix(".flp.bak")
    try:
        shutil.copy2(project, backup)
    except OSError:
        backup = None

    try:
        result = write_notes(project, notes, pattern=pattern, channel=channel, mode=mode)
    except Exception as exc:  # noqa: BLE001 - surface any writer failure as data
        if backup is not None and backup.is_file():
            shutil.copy2(backup, project)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    reloaded = False
    if reload_fl:
        was_running = fl_is_running()
        if was_running:
            _stop_fl()
            time.sleep(2.0)  # let LaunchServices release the dead process
        _open_project(project)
        reloaded = True

    return {
        "ok": True,
        "backend": "project-write",
        "project": str(project),
        "pattern": result.pattern,
        "channel": result.channel,
        "notes_written": result.notes_written,
        "notes_in_pattern": result.notes_in_pattern,
        "reloaded_fl": reloaded,
        "channel_name": next(
            (c["name"] for c in list_channels() if c["index"] == channel),
            f"Channel {channel}",
        ),
    }


def set_tempo(bpm: float, reload_fl: bool = True) -> dict[str, Any]:
    """Change the agent project's tempo and show it in FL."""
    from .flp_writer import set_tempo as write_tempo

    project = agent_project()
    if project is None:
        return {"ok": False, "error": "no agent project configured"}

    backup = project.with_suffix(".flp.bak")
    try:
        shutil.copy2(project, backup)
    except OSError:
        backup = None

    try:
        stored = write_tempo(project, bpm)
    except Exception as exc:  # noqa: BLE001
        if backup is not None and backup.is_file():
            shutil.copy2(backup, project)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    reloaded = False
    if reload_fl:
        if fl_is_running():
            _stop_fl()
            time.sleep(2.0)
        _open_project(project)
        reloaded = True

    return {"ok": True, "tempo": stored, "reloaded_fl": reloaded}


def verify(pattern: int = 1) -> dict[str, Any]:
    """Read the notes back out of the project — ground truth, not a claim."""
    project = agent_project()
    if project is None:
        return {"ok": False, "error": "no agent project configured"}
    try:
        from harness.verify import read_notes

        rows = [r for r in read_notes(project) if r.pattern == pattern]
        return {
            "ok": True,
            "pattern": pattern,
            "note_count": len(rows),
            "notes": [
                {
                    "midi": r.midi,
                    "channel": r.channel,
                    "start_beat": r.start_beat,
                    "length_beats": r.length_beats,
                }
                for r in rows[:64]
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
