"""The boundary probe: try what a confined process must not be able to do.

Run inside the confined process itself (the run kernel at boot, the workspace
tools' op before their first call). The server refuses a process that can read
the workspace canary or reach the instance metadata service. Stdlib only, so
the light workspace op can import it without the SDK.
"""

from __future__ import annotations

import socket
from pathlib import Path

# Cloud instance metadata. The boundary probe asserts the process cannot reach it.
IMDS_ADDRESS = ("169.254.169.254", 80)
IMDS_PROBE_TIMEOUT_SECONDS = 1.0


def probe(canary: Path) -> dict[str, bool]:
    """The probe's two findings; ``wire.BoundaryProbe`` is their validated shape."""
    return {"canary_readable": _can_read(canary), "imds_reachable": _can_connect(IMDS_ADDRESS)}


def _can_read(path: Path) -> bool:
    try:
        path.read_bytes()
    except OSError:
        return False
    return True


def _can_connect(address: tuple[str, int]) -> bool:
    try:
        socket.create_connection(address, timeout=IMDS_PROBE_TIMEOUT_SECONDS).close()
    except OSError:
        return False
    return True
