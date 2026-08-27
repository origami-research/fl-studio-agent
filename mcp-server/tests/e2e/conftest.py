"""Live-FL end-to-end fixtures.

These run the ACTUAL server code (control_midi + FLSession + the merged
controller script) against a real FL Studio session and assert closed-loop
using FL's own read-back - no fake, no human eyeballing. This is the automated
form of "verify first".

They never run in normal CI: the `live_fl` fixture skips unless FL_E2E=1 is set
AND FL Studio is installed, so `pytest tests/` on a CI box (or a dev machine you
don't want hijacked) collects them as skips. Run them deliberately with:

    FL_E2E=1 uv run pytest tests/e2e -m e2e

Lifecycle (docs/14 ordering): open the control-plane ports FIRST, then launch
FL via harness.flctl so it binds them at startup, wait for the controller's
hello, yield a connected FLSession, and stop FL + close ports on teardown. The
trial/welcome modal is dismissed while waiting for hello (a licensed FL skips
the trial dialog; the welcome screen can also be disabled in FL's settings).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO), str(_REPO / "mcp-server" / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

_FL_APP = Path("/Applications/FL Studio 2026.app")


def _osa(*lines: str) -> str:
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="session")
def live_fl():
    if not os.environ.get("FL_E2E"):
        pytest.skip("live-FL e2e disabled; set FL_E2E=1 to run")
    if not _FL_APP.exists():
        pytest.skip("FL Studio not installed")

    from fl_mcp import control_midi
    from fl_mcp.session import FLSession
    from harness import flctl

    ctl = control_midi.get_control()  # ports must exist before FL launches
    if flctl.is_running():
        flctl.stop(force=True)
        time.sleep(1)
    flctl.start()

    hello = None
    deadline = time.time() + 120
    dismissed = 0
    while time.time() < deadline:
        for ev in ctl.drain_events():
            if ev.get("op") == "hello":
                hello = ev
        if hello:
            break
        # Clear the trial/welcome modal a few times while FL boots.
        if dismissed < 5 and _osa(
            'tell application "System Events" to name of first process whose frontmost is true'
        ) == "OsxFL":
            _osa('tell application "System Events" to key code 53')
            dismissed += 1
        time.sleep(1)

    if hello is None:
        flctl.stop(force=True)
        control_midi.close()
        pytest.fail("controller script never said hello within 120s")

    session = FLSession(control=ctl)
    session._e2e_hello = hello  # type: ignore[attr-defined]
    yield session

    flctl.stop(force=True)
    control_midi.close()
