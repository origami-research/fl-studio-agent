"""The boundary probe: try what a confined process must not be able to do.

Run inside the confined process itself (the run kernel at boot, the workspace
tools' op before their first call). The server refuses a process that can read
the workspace canary, create a file beside it (outside ``work``), list the
user's home folder, reach the instance metadata service, or connect to a
listener the server holds open on loopback (where the FL control broker, the
Bridge and every other local service live). Stdlib only, so the light
workspace op can import it without the SDK.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator
from pathlib import Path

# Cloud instance metadata, IPv4 and IPv6. The boundary probe asserts the process cannot reach either.
IMDS_ADDRESSES = (("169.254.169.254", 80), ("fd00:ec2::254", 80))
PROBE_CONNECT_TIMEOUT_SECONDS = 1.0
LOOPBACK_HOST = "127.0.0.1"


def probe(canary: Path, home: Path, loopback_port: int) -> dict[str, bool]:
    """The probe's findings; ``wire.BoundaryProbe`` is their validated shape."""
    return {
        "canary_readable": _can_read(canary),
        "outside_writable": _can_create(canary.with_name(f"probe-write-{os.getpid()}")),
        "home_readable": _can_list(home),
        "imds_reachable": any(_can_connect(address) for address in IMDS_ADDRESSES),
        "loopback_reachable": _can_connect((LOOPBACK_HOST, loopback_port)),
    }


@contextlib.contextmanager
def loopback_listener() -> Iterator[int]:
    """Server side: a loopback port that accepts connections while the probe runs.

    The kernel's own channel to the server is its stdio pipes, so nothing it
    needs is on loopback; a connection here means the boundary lets it reach
    local services.
    """
    with socket.create_server((LOOPBACK_HOST, 0)) as server:
        yield server.getsockname()[1]


def _can_read(path: Path) -> bool:
    try:
        path.read_bytes()
    except OSError:
        return False
    return True


def _can_create(path: Path) -> bool:
    try:
        path.touch(exist_ok=False)
    except OSError:
        return False
    path.unlink()
    return True


def _can_list(path: Path) -> bool:
    try:
        next(path.iterdir(), None)
    except OSError:
        return False
    return True


def _can_connect(address: tuple[str, int]) -> bool:
    try:
        socket.create_connection(address, timeout=PROBE_CONNECT_TIMEOUT_SECONDS).close()
    except OSError:
        return False
    return True
