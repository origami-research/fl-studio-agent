"""``python -I -m fl_mcp.workspace.op``: one workspace operation in the confined process.

The server writes one JSON request on stdin, ``{"op", "root", "args"}``, and
reads one JSON response on stdout: ``{"ok": result}`` or ``{"error":
{"message", "fix"}}``. ``-I`` keeps the workspace and its installed packages
off ``sys.path``, so nothing the model wrote there is imported here.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from fl_mcp.kernel.launch import Workspace
from fl_mcp.workspace.ops import OPS, OpError


def answer(request: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": OPS[request["op"]](Workspace(Path(request["root"])), **request["args"])}
    except OpError as exc:
        return {"error": {"message": exc.message, "fix": exc.fix}}


def main() -> None:
    with contextlib.suppress(OSError):  # an earlier call may have removed it; pip and tempfile need it
        Path(os.environ["TMP"]).mkdir(exist_ok=True)
    response = answer(json.loads(sys.stdin.buffer.read()))
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    # Not a normal exit: a shell op's readers may still be blocked on pipes a
    # leftover process holds, and the server stops that process with the call.
    os._exit(0)


if __name__ == "__main__":
    main()
