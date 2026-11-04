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

ENV_SPOOL_ROOT = "FLMCP_SPOOL_ROOT"
ENV_AGENT_PROJECT = "FLMCP_AGENT_PROJECT"
ENV_FL_LICENSE = "FLMCP_FL_LICENSE"
# Where the run kernel's per-session workspace lives (docs/38). Unset: a fresh temp dir.
ENV_KERNEL_ROOT = "FLMCP_KERNEL_ROOT"
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
    tempo: float | None
    key: str | None
    key_confidence: str | None = None
    note_count: int
    playlist: dict[str, Any]
    automations: list[dict[str, Any]] = Field(default_factory=list)
    project_notes: str | None = None
    channels: list[dict[str, Any]]
    summary: str


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
