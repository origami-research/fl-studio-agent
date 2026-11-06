"""The Windows boundary: the run kernel in a real AppContainer (docs/38).

These start real AppContainer kernels, so they run where asked: CI's windows-sandbox
job sets FLMCP_SANDBOX_TESTS=1 on a Windows runner (after the box contract's firewall
rule for the metadata address) and fails if any is skipped. The orchestration itself
is tested everywhere with fakes in test_kernel_windows.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import Workspace
from fl_mcp.kernel.windows import AppContainerLauncher, CtypesWin32, icacls_path

_on_windows = pytest.mark.platform("win32")
_asked = pytest.mark.skipif(
    os.environ.get("FLMCP_SANDBOX_TESTS") != "1", reason="real AppContainer runs; set FLMCP_SANDBOX_TESTS=1"
)


def sandbox(test):
    return _asked(_on_windows(test))


@pytest.fixture
def host(tmp_path) -> Iterator[KernelHost]:
    workspace = Workspace.create(tmp_path / "kernel")
    launcher = AppContainerLauncher(CtypesWin32(icacls_path(os.environ)), stock=())
    kernel = KernelHost(None, workspace, launcher, os.environ, cell_timeout=120.0)
    yield kernel
    kernel.close()


@sandbox
def test_the_kernel_boots_confined_and_cannot_write_outside_its_workspace(host, tmp_path):
    """Booting at all means the boundary probe passed (canary, home, metadata, loopback)."""
    outside = tmp_path / "outside.txt"
    result = host.run(f"open({str(outside)!r}, 'w')")
    assert result.outcome == "error" and "PermissionError" in (result.traceback or "")
    assert not outside.exists()


@sandbox
def test_devnull_works_in_the_container_though_the_null_device_does_not(host):
    """The arena lockdown check, 2026-11-06: subprocess.DEVNULL raised PermissionError in run.
    os.devnull and DEVNULL are workspace stand-ins (kernel/null_device.py) in the kernel
    and in every Python it starts; NUL itself stays denied."""
    result = host.run(
        "import os, subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'print(1)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)\n"
        "inner = subprocess.run([sys.executable, '-c', 'import subprocess, sys; subprocess.run([sys.executable, \"-V\"], "
        "stdout=subprocess.DEVNULL, check=True); print(repr(sys.stdin.read()))'], stdin=subprocess.DEVNULL, "
        "capture_output=True, text=True)\n"
        "inner = inner.stdout.strip() if inner.returncode == 0 else inner.stderr[:300] + ' ... ' + inner.stderr[-200:]\n"
        "with open(os.devnull, 'w') as sink:\n"
        "    sink.write('discarded')\n"
        "left = [name for name in os.listdir(os.environ['TMP']) if name.startswith('devnull-')]\n"
        "try:\n"
        "    open('NUL', 'w')\n"
        "    device = 'opened'\n"
        "except PermissionError:\n"
        "    device = 'denied'\n"
        "shell = subprocess.run('echo hi > NUL', shell=True, capture_output=True, text=True).returncode\n"
        "inner, left, device, shell != 0"
    )
    assert result.outcome == "ok", result.traceback
    # The child read end of file from DEVNULL, a Python it started used DEVNULL too, no
    # stand-in file outlived its process, and NUL itself (cmd's > NUL) is still refused.
    assert result.result == repr(("''", [], "denied", True))
