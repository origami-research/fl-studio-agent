"""The published MCP contract and its configuration vocabulary."""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.tools.base import Tool
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from fl_mcp import __version__
from fl_mcp.kernel.display import ShownImage, describe
from fl_mcp.schemas import MIXER_HEADING, mixer_line, rounded

ENV_SPOOL_ROOT = "FLMCP_SPOOL_ROOT"
ENV_AGENT_PROJECT = "FLMCP_AGENT_PROJECT"
ENV_FL_LICENSE = "FLMCP_FL_LICENSE"
# Where the run kernel's per-session workspace lives (docs/38). Unset: a fresh temp dir.
ENV_KERNEL_ROOT = "FLMCP_KERNEL_ROOT"
# Seconds one run call may take in all, fl calls included (docs/38). Unset: 600.
ENV_RUN_BUDGET = "FLMCP_RUN_BUDGET_SECONDS"
# The server mode that serves run with no FL session behind it (computer-use runs).
WORKSPACE_ONLY_FLAG = "--workspace-only"
BRIDGE_PORT_FILE = "bridge_udp.json"


class FLLicense(StrEnum):
    """What the FL this engine drives can open.

    A trial FL cannot reopen a project it saved, so under ``trial`` every path
    that makes FL open a saved .flp (the file-lane reload, CLI export) is
    refused before it changes anything. Unset means ``licensed``.
    """

    LICENSED = "licensed"
    TRIAL = "trial"


def fl_license_from_env(environ: Mapping[str, str]) -> FLLicense:
    """Resolve ``FLMCP_FL_LICENSE`` once at the entry point; an unknown value is a setup error."""
    value = environ.get(ENV_FL_LICENSE, FLLicense.LICENSED.value)
    try:
        return FLLicense(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in FLLicense)
        raise ValueError(f"{ENV_FL_LICENSE} must be one of {allowed}; got {value!r}") from exc


def server_description(confined: bool) -> str:
    """The MCP serverInfo description. Its ``confined=`` says whether the OS confines
    model code (run) to the session workspace on this host."""
    return f"FL Studio MCP {build_version()}; confined={'true' if confined else 'false'}"


class BridgePortFile(BaseModel):
    """Endpoint published by the native Bridge in each session spool."""

    proto: int
    transport: int = 1
    port: int = Field(ge=1, le=65535)
    token: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]+$")
    pid: int | None = None


class ToolErrorResult(BaseModel):
    status: Literal["error"] = "error"
    error: str
    message: str
    fix: str
    retryable: bool
    candidates: list[str] | None = None


# Every tool result carries what the engine did during the call (fl_mcp/actions.py).
Actions = Annotated[
    list[str],
    Field(
        description="What happened in FL during this call, in order: one plain sentence per change "
        "FL confirmed or measurement taken. Reads and failures add none."
    ),
]


# The images a result carries go to the model as MCP image content; the result names them.
ShownImages = Annotated[
    list[str],
    Field(
        description="One line per image shown, with its size and token cost; the images "
        "themselves follow this result as image content."
    ),
]


# ok: the cell finished. error: it raised (traceback). interrupted: the run reached its
# budget (fl calls included) and the cell was stopped between fl calls, the kernel kept its state. kernel_lost: the kernel
# process died or was killed, its state is gone and the next run starts fresh.
RunOutcome = Literal["ok", "error", "interrupted", "kernel_lost"]


class RunResult(BaseModel):
    outcome: RunOutcome
    stdout: str
    result: str | None = None
    traceback: str | None = None
    # Every fl call the cell made that FL confirmed, including those before an error.
    actions: Actions
    images: ShownImages = Field(default_factory=list)
    # The images themselves: they reach the model as MCP image content, never as text here.
    _shown: list[ShownImage] = PrivateAttr(default_factory=list)
    _dropped: int = PrivateAttr(default=0)

    @property
    def shown(self) -> list[ShownImage]:
        return self._shown

    @property
    def dropped(self) -> int:
        return self._dropped

    def with_images(self, shown: list[ShownImage], dropped: int) -> RunResult:
        """This result carrying the images a cell showed, and naming them."""
        result = self.model_copy(update={"images": describe(shown, dropped)})
        result._shown, result._dropped = list(shown), dropped
        return result

    @model_validator(mode="after")
    def _outcome_matches_payload(self) -> RunResult:
        if (self.outcome == "ok") != (self.traceback is None):
            raise ValueError("a run result has a traceback exactly when its outcome is not ok")
        if self.result is not None and self.outcome != "ok":
            raise ValueError("only an ok run result carries a result")
        return self


class SceneNote(BaseModel):
    channel: int
    pattern: int
    key: str
    midi: int
    start: float
    length: float
    velocity: float


class SceneSummary(BaseModel):
    """fl.scene(): the whole project in one digest. print(fl.scene()) reads it: the
    project's notes, tempo and key, the playlist, then a line per pattern (length, where
    the playlist plays it, notes by channel), per channel (mixer track, volume, what its
    notes span, what the project's notes say it is for) and per mixer track in use
    (volume, effects, sends), then the automation. It is an object, not a string: read a
    part by its field (scene.channels, scene.mixer) rather than slicing printed text. An
    object's detail is its own read: fl.track(channel).notes(), fl.playlist, fl.mixer."""

    tempo: float
    key: str | None
    key_confidence: str | None = None
    note_count: int
    project_notes: str | None = None
    playlist: dict[str, Any]
    patterns: list[dict[str, Any]]
    channels: list[dict[str, Any]]
    mixer: list[dict[str, Any]]
    automations: list[dict[str, Any]] = Field(default_factory=list)

    def __str__(self) -> str:
        tempo = f"tempo {self.tempo:g}"
        lines = [f"project notes: {self.project_notes}"] if self.project_notes else []
        if self.note_count:
            lines.append(f"{tempo}, key {self.key} (confidence: {self.key_confidence}), {self.note_count} notes")
        else:
            lines.append(f"{tempo}, no notes yet")
        lines.append(_playlist_line(self.playlist))
        several = len(self.playlist.get("arrangements", [])) > 1
        if self.patterns:
            lines.append("patterns (number 'name': length; beats the playlist plays it from; notes on channels):")
            lines += [f"  {_pattern_line(p, several)}" for p in self.patterns]
        if self.channels:
            lines.append(
                "channels (index name -> mixer track: volume; notes of all its patterns together, "
                "beats within a pattern; what the project's notes say it is for):"
            )
            lines += [f"  {_channel_line(c, several)}" for c in self.channels]
        if self.mixer:
            lines.append(MIXER_HEADING)
            lines += [f"  {_mixer_line(t)}" for t in self.mixer]
        lines += [
            f"automation: {a['name']} ({a['points']} pts over {a['span_beats']:g} beats, "
            f"{a['values'][0]:.2f} -> {a['values'][1]:.2f})"
            for a in self.automations
        ]
        return "\n".join(lines)

    __repr__ = __str__


def _beats(values: list[float]) -> str:
    return " ".join(f"{v:g}" for v in values)


def _arrangement(number: int | None) -> str:
    return "the clips added this session" if number is None else f"arrangement {number}"


def _placed(placements: list[dict[str, Any]], several: bool) -> str:
    """Start beats, named by arrangement only when the song has several."""
    if not several:
        return _beats(placements[0]["beats"])
    return ", ".join(f"{_beats(p['beats'])} in {_arrangement(p['arrangement'])}" for p in placements)


def _runs(numbers: list[int]) -> str:
    """Sorted numbers with each run of three or more as one range: 1-16, or 0 2 5-9."""
    runs: list[list[int]] = []
    for n in numbers:
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return " ".join(f"{r[0]}-{r[-1]}" if len(r) > 2 else " ".join(map(str, r)) for r in runs)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _playlist_line(playlist: dict[str, Any]) -> str:
    if not playlist.get("clip_count"):
        return "playlist: empty (no clips placed on the arrangement)"
    line = f"playlist: {_count(playlist['clip_count'], 'clip')}"
    if playlist["tracks_used"]:
        line += f" on {_count(playlist['tracks_used'], 'track')}"
    if playlist.get("untracked"):
        line += f" ({playlist['untracked']} on a track FL did not report)"
    spans = playlist["arrangements"]
    if len(spans) == 1:
        start, end = spans[0]["span_beats"]
        return f"{line}, beats {start:g}-{end:g}"
    return f"{line}; " + "; ".join(
        f"{_arrangement(a['arrangement'])}: {_count(a['clip_count'], 'clip')}, "
        f"beats {a['span_beats'][0]:g}-{a['span_beats'][1]:g}"
        for a in spans
    )


def _pattern_line(pattern: dict[str, Any], several: bool) -> str:
    beats = "length unknown" if pattern["beats"] is None else f"{pattern['beats']:g} beats"
    at = f"at {_placed(pattern['at'], several)}" if pattern["at"] else "not in the playlist"
    notes = f"{_count(pattern['notes'], 'note')} on {_runs(pattern['channels'])}" if pattern["notes"] else "no notes"
    name = "(name unknown)" if pattern["name"] is None else repr(pattern["name"])
    return f"{pattern['number']} {name}: {beats}; {at}; {notes}"


def _channel_line(channel: dict[str, Any], several: bool) -> str:
    track = "" if channel.get("mixer_track") is None else f" -> {channel['mixer_track']}"
    if channel["volume"] is None:
        levels = "not in FL's channel list, levels unknown"
    else:
        levels = f"vol {rounded(channel['volume'])}"
        if round(channel["pan"], 2):
            levels += f", pan {rounded(channel['pan'])}"
        if channel["muted"]:
            levels += ", muted"
    if channel["notes"]:
        low, high = channel["range"]
        pitches = low if low == high else f"{low}-{high}, pitches {' '.join(channel['pitch_classes'])}"
        notes = f"{_count(channel['notes'], 'note')}, beats {channel['beats'][0]:g}-{channel['beats'][1]:g}, {pitches}"
        if channel.get("root_motion"):
            notes += f", root motion {' '.join(channel['root_motion'])}"
    else:
        notes = "no notes"
    if channel.get("clips_at"):
        notes += f", playlist clips at {_placed(channel['clips_at'], several)}"
    about = f"; {channel['about']}" if channel.get("about") else ""
    return f"{channel['index']} {channel['name']}{track}: {levels}; {notes}{about}"


def _mixer_line(track: dict[str, Any]) -> str:
    return mixer_line(
        track["index"],
        track["name"],
        track["volume"],
        track["pan"],
        [flag for flag in ("muted", "solo") if track[flag]],
        [(e["slot"], e["plugin"]) for e in track["effects"]],
        [(s["target"], s["level"]) for s in track["sends"]],
    )


class SceneNotesResult(BaseModel):
    notes: list[SceneNote]


def build_version() -> str:
    """Package version plus the checked-out revision when available.

    A wheel built by scripts/build-wheel.sh already names its commit in its
    ``+g<sha>`` local version; git is consulted only for unstamped checkouts,
    never to relabel an installed build from whatever repository surrounds it.
    """
    if "+" in __version__:
        return __version__
    try:
        parents = subprocess.check_output(
            ["git", "show", "-s", "--format=%P", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            stderr=subprocess.DEVNULL,
            text=True,
        ).split()
        # GitHub checks a synthetic merge commit. Its second parent is the PR
        # head, whose last source revision is the artifact's real producer.
        revision = "HEAD^2" if len(parents) == 2 else "HEAD"
        commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h", revision, "--", "mcp-server/src"],
            cwd=Path(__file__).resolve().parents[3],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return __version__
    return f"{__version__}+{commit}" if commit else __version__


def build_contract() -> dict[str, Any]:
    """Build the one machine-readable engine contract."""
    from fl_mcp.server import run_description, server_instructions, tool_definitions

    return {
        # The checked-in contract must survive a merge unchanged. Runtime
        # provenance belongs in the MCP server description, not this artifact.
        "version": __version__,
        "env": {
            ENV_SPOOL_ROOT: ENV_SPOOL_ROOT,
            ENV_AGENT_PROJECT: ENV_AGENT_PROJECT,
            ENV_FL_LICENSE: [member.value for member in FLLicense],
            ENV_KERNEL_ROOT: ENV_KERNEL_ROOT,
            ENV_RUN_BUDGET: ENV_RUN_BUDGET,
            "BRIDGE_PORT_FILE": BRIDGE_PORT_FILE,
            "bridge_port_file_schema": BridgePortFile.model_json_schema(),
        },
        "tools": [_tool_contract(fn) for fn in tool_definitions()],
        "server": {
            "workspace_only_flag": WORKSPACE_ONLY_FLAG,
            "description": "serverInfo.description ends with '; confined=true' or '; confined=false'",
            "instructions": server_instructions(with_fl=True),
            "trial": {
                "run_description": run_description(fl_license=FLLicense.TRIAL),
            },
            "workspace_only": {
                "instructions": server_instructions(with_fl=False),
                "run_description": run_description(fl_license=None),
            },
        },
        "errors": {
            "categories": list(_error_categories()),
            "shape": ToolErrorResult.model_json_schema(),
            "delivery": "ToolErrorResult JSON is delivered as the text content of isError tool results.",
        },
    }


def contract_json() -> str:
    return json.dumps(build_contract(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    print(contract_json(), end="")


def _tool_contract(fn: Callable[..., Any]) -> dict[str, Any]:
    tool = Tool.from_function(fn)
    return {
        "name": tool.name,
        "description": inspect.cleandoc(fn.__doc__ or ""),
        "inputSchema": tool.parameters,
        "outputSchema": tool.output_schema,
    }


def _error_categories() -> tuple[str, ...]:
    from fl_mcp.errors import ErrorCategory

    return ErrorCategory.__args__
