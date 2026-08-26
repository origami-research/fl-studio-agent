"""Spool-first control transport (issue #8).

The server side of the file-based control plane: write a command file the
controller script's OnIdle loop picks up, then poll for its response file.
No virtual MIDI, no rtmidi, no separate bridge process - the server is the
only host-side process.

Mirrors the SysEx control protocol's request/response shape exactly
(``{"id", "op", "args", "kwargs"}`` -> ``{"id", "ok", "ret"|"err"}``), so the
dispatch path inside FL is identical regardless of transport.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from pathlib import Path
from typing import Any

from . import flpaths

CMD_PREFIX = "ctl_cmd_"
RESP_PREFIX = "ctl_resp_"

_ids = itertools.count(1)


class ControlUnavailable(RuntimeError):
    """The control spool directory does not exist (FL not installed / no scripts)."""


def _spool_root() -> Path:
    scripts = flpaths.piano_roll_scripts_dir()
    if scripts is None:
        raise ControlUnavailable(
            "Could not find FL Studio's control spool "
            "(~/Documents/Image-Line/FL Studio/Settings/Piano roll scripts/FLMCP). "
            "Is FL Studio installed and the controller script deployed?"
        )
    # The controller script prefers the FLMCP subdir; match its write target.
    root = scripts / "FLMCP"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_atomic(path: Path, obj: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def call(op: str, args: list | None = None, kwargs: dict | None = None, timeout: float = 5.0) -> dict:
    """Send one control op and block until its response lands (or timeout).

    Returns the controller script's response dict (``{"id", "ok", "ret"|"err"}``).
    Raises TimeoutError if no response file appears within ``timeout`` seconds -
    the usual cause is the controller script not being loaded/assigned in FL.
    """
    root = _spool_root()
    req_id = next(_ids)
    cmd_path = root / f"{CMD_PREFIX}{req_id}.json"
    resp_path = root / f"{RESP_PREFIX}{req_id}.json"
    # Clear any stale response from a previous run reusing this id.
    for p in (resp_path,):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    _write_atomic(cmd_path, {"id": req_id, "op": op, "args": args or [], "kwargs": kwargs or {}})

    deadline = time.time() + timeout
    while time.time() < deadline:
        if resp_path.exists():
            try:
                response = json.loads(resp_path.read_text())
            except (ValueError, OSError):
                time.sleep(0.02)
                continue
            # Server is not sandboxed: clean up both files so the spool stays small.
            for p in (resp_path, cmd_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            return response
        time.sleep(0.02)
    # Leave the command file for post-mortem; it will be tombstoned if FL wakes.
    raise TimeoutError(
        f"no response to control op {op!r} (id {req_id}) within {timeout}s - "
        "is the FLMCP controller script loaded in FL Studio?"
    )
