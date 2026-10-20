"""The published MCP contract and its configuration vocabulary."""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver.tools.base import Tool
from pydantic import BaseModel, Field, model_validator

from fl_mcp import __version__
from fl_mcp.schemas import RenderSpan

ENV_SPOOL_ROOT = "FLMCP_SPOOL_ROOT"
ENV_AGENT_PROJECT = "FLMCP_AGENT_PROJECT"
ENV_FL_LICENSE = "FLMCP_FL_LICENSE"
# Where the run kernel's per-session workspace lives (docs/38). Unset: a fresh temp dir.
ENV_KERNEL_ROOT = "FLMCP_KERNEL_ROOT"
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


# ok: the cell finished. error: it raised (traceback). interrupted: it ran past its
# time budget and was stopped, the kernel kept its state. kernel_lost: the kernel
# process died or was killed, its state is gone and the next run starts fresh.
RunOutcome = Literal["ok", "error", "interrupted", "kernel_lost"]


class RunResult(BaseModel):
    outcome: RunOutcome
    stdout: str
    result: str | None = None
    traceback: str | None = None

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

    @model_validator(mode="after")
    def _summary_or_notes(self) -> GetSceneResult:
        if (self.notes is None) == (self.summary is None):
            raise ValueError("get_scene result requires exactly one of summary or notes")
        return self

    @classmethod
    def from_summary(cls, summary: SceneSummary) -> GetSceneResult:
        return cls(**summary.model_dump())

    @classmethod
    def from_notes(cls, notes: SceneNotesResult) -> GetSceneResult:
        return cls(notes=notes.notes)


class RenderResult(BaseModel):
    path: str
    wavBase64: str | None = None
    note: str | None = None
    # The live length the capture recorded and where FL read it (song/pattern).
    span: RenderSpan


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
    from fl_mcp.server import tool_definitions

    return {
        # The checked-in contract must survive a merge unchanged. Runtime
        # provenance belongs in the MCP server description, not this artifact.
        "version": __version__,
        "env": {
            ENV_SPOOL_ROOT: ENV_SPOOL_ROOT,
            ENV_AGENT_PROJECT: ENV_AGENT_PROJECT,
            ENV_FL_LICENSE: [member.value for member in FLLicense],
            ENV_KERNEL_ROOT: ENV_KERNEL_ROOT,
            "BRIDGE_PORT_FILE": BRIDGE_PORT_FILE,
            "bridge_port_file_schema": BridgePortFile.model_json_schema(),
        },
        "tools": [_tool_contract(fn) for fn in tool_definitions()],
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
