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
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fl_mcp import __version__
from fl_mcp.audio import AudioAnalysis
from fl_mcp.schemas import CheckResult, RenderSpan

ENV_SPOOL_ROOT = "FLMCP_SPOOL_ROOT"
ENV_AGENT_PROJECT = "FLMCP_AGENT_PROJECT"
ENV_FL_LICENSE = "FLMCP_FL_LICENSE"
# Where the run kernel's per-session workspace lives (docs/38). Unset: a fresh temp dir.
ENV_KERNEL_ROOT = "FLMCP_KERNEL_ROOT"
# "1" adds the workspace tools (docs/39) to the FL tools; unset or "0" leaves the surface as it was.
ENV_WORKSPACE_TOOLS = "FLMCP_WORKSPACE_TOOLS"
# The server mode that serves only the workspace tools, with no FL session (docs/39).
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


def workspace_tools_from_env(environ: Mapping[str, str]) -> bool:
    """Resolve ``FLMCP_WORKSPACE_TOOLS`` once at the entry point; an unknown value is a setup error."""
    value = environ.get(ENV_WORKSPACE_TOOLS, "0")
    if value not in ("0", "1"):
        raise ValueError(f"{ENV_WORKSPACE_TOOLS} must be 0 or 1; got {value!r}")
    return value == "1"


def server_description(confined: bool) -> str:
    """The MCP serverInfo description. Its ``confined=`` says whether the OS confines
    model code (run and the workspace tools) to the session workspace on this host."""
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


# ok: the cell finished. error: it raised (traceback). interrupted: it ran past its
# time budget and was stopped, the kernel kept its state. kernel_lost: the kernel
# process died or was killed, its state is gone and the next run starts fresh.
RunOutcome = Literal["ok", "error", "interrupted", "kernel_lost"]


class RunResult(BaseModel):
    outcome: RunOutcome
    stdout: str
    result: str | None = None
    traceback: str | None = None
    # Every fl call the cell made that FL confirmed, including those before an error.
    actions: Actions

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
    tempo: float | None
    key: str | None
    key_confidence: str | None = None
    note_count: int
    playlist: dict[str, Any]
    automations: list[dict[str, Any]] = Field(default_factory=list)
    channels: list[dict[str, Any]]
    summary: str


class SceneNotesResult(BaseModel):
    notes: list[SceneNote]


class GetSceneResult(BaseModel):
    """One object for both detail modes.

    MCP requires every tool's output schema to be an object, so a root-level
    union (summary | notes) is not expressible: the SDK rejected its own
    tools/list result and no client could list tools. detail="summary" fills
    the summary fields; detail="notes" fills ``notes``.
    """

    tempo: float | None = None
    key: str | None = None
    key_confidence: str | None = None
    note_count: int | None = None
    playlist: dict[str, Any] | None = None
    automations: list[dict[str, Any]] | None = None
    channels: list[dict[str, Any]] | None = None
    summary: str | None = None
    notes: list[SceneNote] | None = None
    actions: Actions  # a read: always empty

    @model_validator(mode="after")
    def _summary_or_notes(self) -> GetSceneResult:
        if (self.notes is None) == (self.summary is None):
            raise ValueError("get_scene result requires exactly one of summary or notes")
        return self

    @classmethod
    def from_summary(cls, summary: SceneSummary) -> GetSceneResult:
        return cls(**summary.model_dump(), actions=[])

    @classmethod
    def from_notes(cls, notes: SceneNotesResult) -> GetSceneResult:
        return cls(notes=notes.notes, actions=[])


class RenderResult(BaseModel):
    """Where the render's WAV is and what it recorded. The audio itself is only in the file."""

    path: str = Field(description="The rendered WAV file.")
    bytes: int = Field(ge=0, description="The WAV file's size in bytes.")
    span: RenderSpan = Field(description="What played (song or pattern), its live beats and tempo, and the seconds recorded.")
    actions: Actions


# The check and analyze tools return what fl.check() and fl.analyze() do plus
# the call's actions. They keep the published names those results already had,
# so the contract's type names do not change under its consumers.
class CheckToolResult(CheckResult):
    """What check heard: each channel, the master's peak, and a summary line."""

    model_config = ConfigDict(title="CheckResult")
    actions: Actions


class AnalyzeResult(AudioAnalysis):
    """Measurements of a rendered WAV."""

    model_config = ConfigDict(title="AudioAnalysis")
    actions: Actions


# -- the workspace tools (docs/39) ----------------------------------------------------

WorkspaceActions = Annotated[
    list[str],
    Field(
        description="What this call changed, in plain sentences: the command it ran, or the file it "
        "wrote or edited. Reads add none."
    ),
]


class ShellResult(BaseModel):
    """One command's outcome; a non-zero exit_code is a result, not an error."""

    exit_code: int | None = Field(description="The command's exit code; null when it timed out.")
    timed_out: bool
    stdout: str
    stderr: str
    truncated: bool = Field(description="Output was cut to its start and end; full_output has all of it.")
    full_output: str | None = Field(
        description="The workspace file holding the whole output when it was cut."
    )
    duration_s: float
    note: str | None = None
    actions: WorkspaceActions


FileKind = Literal["text", "midi", "audio", "image", "fl_song", "archive", "binary"]


class ReadFileResult(BaseModel):
    """A text file's numbered lines, or a binary file's size and kind with a hint."""

    path: str
    kind: FileKind
    bytes: int
    content: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    total_lines: int | None = None
    truncated: bool = False
    hint: str | None = None
    note: str | None = None
    actions: WorkspaceActions  # a read: always empty

    @model_validator(mode="after")
    def _text_has_content(self) -> ReadFileResult:
        if (self.kind == "text") != (self.content is not None):
            raise ValueError("a read_file result has content exactly when the file is text")
        return self


class WriteFileResult(BaseModel):
    path: str
    bytes: int
    created: bool
    actions: WorkspaceActions


class EditFileResult(BaseModel):
    path: str
    replacements: int
    actions: WorkspaceActions


class GlobResult(BaseModel):
    paths: list[str] = Field(description="Matching files, newest first.")
    truncated: bool
    note: str | None = None
    actions: WorkspaceActions  # a read: always empty


GrepMode = Literal["files", "lines", "count"]


class GrepResult(BaseModel):
    mode: GrepMode
    matches: list[str] = Field(
        description='files: paths, newest first; lines: "path:line: text"; count: "path: N".'
    )
    truncated: bool
    note: str | None = None
    actions: WorkspaceActions  # a read: always empty


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
    from fl_mcp.server import tool_definitions, workspace_tool_definitions

    return {
        # The checked-in contract must survive a merge unchanged. Runtime
        # provenance belongs in the MCP server description, not this artifact.
        "version": __version__,
        "env": {
            ENV_SPOOL_ROOT: ENV_SPOOL_ROOT,
            ENV_AGENT_PROJECT: ENV_AGENT_PROJECT,
            ENV_FL_LICENSE: [member.value for member in FLLicense],
            ENV_KERNEL_ROOT: ENV_KERNEL_ROOT,
            ENV_WORKSPACE_TOOLS: ["0", "1"],
            "BRIDGE_PORT_FILE": BRIDGE_PORT_FILE,
            "bridge_port_file_schema": BridgePortFile.model_json_schema(),
        },
        "tools": [_tool_contract(fn) for fn in tool_definitions()],
        # Served beside "tools" when FLMCP_WORKSPACE_TOOLS=1, and alone under --workspace-only.
        "workspace_tools": [_tool_contract(fn) for fn in workspace_tool_definitions()],
        "server": {
            "workspace_only_flag": WORKSPACE_ONLY_FLAG,
            "description": "serverInfo.description ends with '; confined=true' or '; confined=false'",
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
