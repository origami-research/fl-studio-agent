"""The kernel wire: newline-delimited JSON between the MCP server and its kernel.

The kernel runs model code, so the server treats everything it sends as
untrusted data: messages are JSON validated against these models (never
pickle), and a session call may only name a method in ``SESSION_METHODS``,
with arguments validated against ``FLSession``'s own signature. The same
signature types both directions, so there is one schema for the RPC and it
cannot drift from the session it fronts.
"""

from __future__ import annotations

import functools
import inspect
from typing import IO, Annotated, Any, Literal, get_args, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, create_model

from fl_mcp import errors
from fl_mcp.contract import RunResult
from fl_mcp.errors import ErrorCategory, ToolError
from fl_mcp.session import FLSession

# The FLSession surface that sdk.FL and sdk.Track call; a test pins it to sdk.py.
SessionMethod = Literal[
    "add_effect",
    "channel_names",
    "check",
    "clear_notes",
    "export_offline",
    "plugin_params",
    "read_notes",
    "render_wav",
    "save_project",
    "set_automation",
    "set_channel_levels",
    "set_plugin_param",
    "set_tempo",
    "snapshot",
    "tempo",
    "undo",
    "write_notes",
]
SESSION_METHODS = frozenset(get_args(SessionMethod))
# FLSession.file_edit is a context manager; the wire carries its two halves.
FILE_EDIT_ENTER = "file_edit_enter"
FILE_EDIT_EXIT = "file_edit_exit"
CallMethod = SessionMethod | Literal["file_edit_enter", "file_edit_exit"]
# Effect carries opaque plugin bytes; JSON needs them as base64.
_BYTES_AS_BASE64 = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")


class WireError(BaseModel):
    category: ErrorCategory
    message: str
    fix: str
    retryable: bool = False
    candidates: list[str] | None = None

    @classmethod
    def from_tool_error(cls, exc: ToolError) -> WireError:
        return cls(
            category=exc.category,
            message=exc.message,
            fix=exc.fix,
            retryable=exc.retryable,
            candidates=exc.candidates,
        )

    def to_tool_error(self) -> ToolError:
        return ToolError(
            self.category, self.message, self.fix, retryable=self.retryable, candidates=self.candidates
        )


# -- server -> kernel ---------------------------------------------------------


class Exec(BaseModel):
    op: Literal["exec"] = "exec"
    code: str


class Interrupt(BaseModel):
    op: Literal["interrupt"] = "interrupt"


class Reply(BaseModel):
    op: Literal["reply"] = "reply"
    id: int
    value: Any = None
    error: WireError | None = None


# -- kernel -> server ---------------------------------------------------------


class BoundaryProbe(BaseModel):
    """What the kernel could reach that its OS boundary must deny."""

    canary_readable: bool
    imds_reachable: bool


class Ready(BaseModel):
    op: Literal["ready"] = "ready"
    pid: int
    probe: BoundaryProbe | None = None


class Call(BaseModel):
    op: Literal["call"] = "call"
    # Pairs the reply with its call: an interrupt can abandon a call mid-wait,
    # and its late reply must not answer the next one.
    id: int = 0
    method: CallMethod
    args: dict[str, Any] = Field(default_factory=dict)


class Done(BaseModel):
    op: Literal["done"] = "done"
    result: RunResult


ToKernel = Annotated[Exec | Interrupt | Reply, Field(discriminator="op")]
FromKernel = Annotated[Ready | Call | Done, Field(discriminator="op")]
_TO_KERNEL: TypeAdapter[Exec | Interrupt | Reply] = TypeAdapter(ToKernel)
_FROM_KERNEL: TypeAdapter[Ready | Call | Done] = TypeAdapter(FromKernel)


def encode(message: BaseModel) -> bytes:
    return message.model_dump_json().encode() + b"\n"


def send(stream: IO[bytes], message: BaseModel) -> None:
    stream.write(encode(message))
    stream.flush()


def parse_to_kernel(line: bytes) -> Exec | Interrupt | Reply:
    return _TO_KERNEL.validate_json(line)


def parse_from_kernel(line: bytes) -> Ready | Call | Done:
    return _FROM_KERNEL.validate_json(line)


# -- the FLSession call codec ---------------------------------------------------


@functools.cache
def _schema(method: str) -> tuple[inspect.Signature, type[BaseModel], type[BaseModel]]:
    """Argument and return models built from FLSession's own annotations."""
    function = getattr(FLSession, method)
    hints = get_type_hints(function)
    signature = inspect.signature(function)
    fields: dict[str, Any] = {
        name: (hints[name], ... if param.default is inspect.Parameter.empty else param.default)
        for name, param in signature.parameters.items()
        if name != "self"
    }
    args_model = create_model(f"{method}_args", __config__=_BYTES_AS_BASE64, **fields)
    result_model = create_model(f"{method}_result", __config__=_BYTES_AS_BASE64, value=(hints["return"], ...))
    return signature, args_model, result_model


def encode_call(method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Call:
    """Kernel side: bind an SDK call's arguments by name and serialize them."""
    signature, args_model, _ = _schema(method)
    bound = signature.bind(None, *args, **kwargs).arguments
    del bound["self"]
    return Call.model_validate({"method": method, "args": args_model(**bound).model_dump(mode="json")})


def decode_call(call: Call) -> dict[str, Any]:
    """Server side: validate a call's arguments into the session's own types."""
    _, args_model, _ = _schema(call.method)
    try:
        return dict(args_model.model_validate(call.args))
    except ValidationError as exc:
        raise errors.invalid_argument(f"fl ({call.method})", str(exc)) from exc


def encode_result(method: str, value: Any) -> Any:
    return _schema(method)[2](value=value).model_dump(mode="json")["value"]


def decode_result(method: str, value: Any) -> Any:
    return _schema(method)[2].model_validate({"value": value}).value
