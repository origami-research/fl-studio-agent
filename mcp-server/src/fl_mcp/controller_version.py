"""The controller protocol version derived from the installable script.

The controller script ships inside this package (``fl_mcp.fl_scripts``) and is
the only source of this value.  Reading its ``SCRIPT_VER`` keeps the server's expectation and
what ``fl-mcp-install`` deploys from drifting apart.
"""

from __future__ import annotations

import re
from importlib.resources import files

_SCRIPT_VER = re.compile(r'^SCRIPT_VER = "(?P<version>[^"]+)"$', re.MULTILINE)


def controller_script_bytes() -> bytes:
    """The controller bytes that ``fl-mcp-install`` deploys."""
    return files("fl_mcp.fl_scripts").joinpath("device_FLMCP.py").read_bytes()


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
