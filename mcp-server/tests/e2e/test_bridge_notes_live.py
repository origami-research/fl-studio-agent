"""Live bridge note results against one disposable FL session.

Run deliberately with ``FL_E2E=1``.  This starts FL through the dev-session
owner, which also tears it down; it refuses to overlap another FL session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from fl_mcp.fl.paths import fl_app
from fl_mcp.fl.process import fl_process_running

pytestmark = pytest.mark.e2e


def test_bridge_rewrite_receipts_follow_live_state() -> None:
    """Replace and merge the same live roll after it already has a note."""
    if not os.environ.get("FL_E2E"):
        pytest.skip("live-FL e2e disabled; set FL_E2E=1 to run")
    if not fl_app().exists():
        pytest.skip("FL Studio not installed")
    if fl_process_running():
        pytest.skip("bridge receipt probe requires FL Studio closed before it starts")

    source = '''
import json
from fl_mcp.schemas import Note

track = fl.track(0)
track.write([Note(key=48, start=0, length=1)], mode="merge")
replaced = track.write([Note(key=60, start=0, length=1)], mode="replace")
merged = track.write([Note(key=64, start=1, length=1)], mode="merge")
backend = fl._session.store.last_action().backend
read = [n.key for n in track.notes()]
print("BRIDGE_RECEIPTS=" + json.dumps([replaced.model_dump(), merged.model_dump(), backend, read]))
'''
    completed = subprocess.run(
        [sys.executable, "-m", "fl_mcp.dev_session", "--exec", source],
        capture_output=True,
        check=False,
        text=True,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    line = next((line for line in completed.stdout.splitlines() if line.startswith("BRIDGE_RECEIPTS=")), None)
    assert line is not None, completed.stdout
    replaced, merged, backend, read = json.loads(line.removeprefix("BRIDGE_RECEIPTS="))
    assert backend == "bridge"
    assert [replaced["total"], merged["total"]] == [1, 2]
    assert read == [60, 64], "an immediate read returns the written notes"
