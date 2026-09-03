"""The published MCP contract and its configuration vocabulary."""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.tools.base import Tool
from pydantic import BaseModel, Field, RootModel, model_validator

from fl_mcp import __version__
from fl_mcp.schemas import Note, Target

ENV_SPOOL_ROOT = "FLMCP_SPOOL_ROOT"
ENV_AGENT_PROJECT = "FLMCP_AGENT_PROJECT"
BRIDGE_PORT_FILE = "bridge_udp.json"


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


class RunResult(BaseModel):
    stdout: str
    result: str | None = None
    traceback: str | None = None

    @model_validator(mode="after")
    def _has_one_outcome(self) -> RunResult:
        if self.traceback is not None and self.result is not None:
            raise ValueError("run result cannot contain both result and traceback")
        return self


class NotesSummary(BaseModel):
    keys: dict[Literal["min", "max"], int]
    span_beats: dict[Literal["start", "end"], float]
    sample: list[Note]
    note: str


class GetNotesResult(BaseModel):
    target: Target
    provenance: str | None
    as_of: str | None
    note_count: int
    notes: list[Note] | None = None
    summary: NotesSummary | None = None

    @model_validator(mode="after")
    def _has_notes_or_summary(self) -> GetNotesResult:
        if (self.notes is None) == (self.summary is None):
            raise ValueError("get_notes result requires exactly one of notes or summary")
        return self


class SceneNote(BaseModel):
    channel: int
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


class GetSceneResult(RootModel[Annotated[SceneSummary | SceneNotesResult, Field(union_mode="left_to_right")]]):
    pass


class RenderResult(BaseModel):
    path: str
    wavBase64: str | None = None
    note: str | None = None


def build_version() -> str:
    """Package version plus the checked-out revision when available."""
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
    from fl_mcp.server import tool_definitions

    return {
        # The checked-in contract must survive a merge unchanged. Runtime
        # provenance belongs in the MCP server description, not this artifact.
        "version": __version__,
        "env": {
            ENV_SPOOL_ROOT: ENV_SPOOL_ROOT,
            ENV_AGENT_PROJECT: ENV_AGENT_PROJECT,
            "BRIDGE_PORT_FILE": BRIDGE_PORT_FILE,
            "bridge_port_file_schema": BridgePortFile.model_json_schema(),
        },
        "tools": [_tool_contract(fn) for fn in tool_definitions()],
        "errors": {
            "categories": list(_error_categories()),
            "shape": ToolErrorResult.model_json_schema(),
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
