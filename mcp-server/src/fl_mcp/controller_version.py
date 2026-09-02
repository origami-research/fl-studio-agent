"""The controller protocol version derived from the installable script.

The controller script is bundled with the server wheel and is the only source
of this value.  Reading its ``SCRIPT_VER`` keeps the server's expectation and
what ``fl-mcp-install`` deploys from drifting apart.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

_SCRIPT_VER = re.compile(r'^SCRIPT_VER = "(?P<version>[^"]+)"$', re.MULTILINE)
_BUNDLED_CONTROLLER = "_controller/device_FLMCP.py"


def controller_script_bytes() -> bytes:
    """The controller bytes that ``fl-mcp-install`` deploys."""
    bundled = files("fl_mcp").joinpath(_BUNDLED_CONTROLLER)
    if bundled.is_file():
        return bundled.read_bytes()
    source = Path(__file__).resolve().parents[3] / "fl-scripts" / "controller" / "device_FLMCP.py"
    return source.read_bytes()


def _controller_protocol_version() -> str:
    source = controller_script_bytes().decode("utf-8")
    match = _SCRIPT_VER.search(source)
    if match is None:
        raise RuntimeError("controller script has no SCRIPT_VER")
    return match["version"]


CONTROLLER_PROTOCOL_VERSION = _controller_protocol_version()


def controller_protocol_matches(script_ver: str) -> bool:
    """Whether a hello came from the controller this server installs."""
    return script_ver == CONTROLLER_PROTOCOL_VERSION
