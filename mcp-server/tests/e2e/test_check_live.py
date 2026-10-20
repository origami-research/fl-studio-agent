"""fl.check() against real FL: the meters hear a written part and not an empty one.

Run deliberately with ``FL_E2E=1``. This starts FL through the dev-session
owner, which also tears it down; it refuses to overlap another FL session.
The fast suite (tests/test_check.py) simulates the meters; this is the only
place their real readings, the absolute-tick play head and the poll cadence
against short drum hits are proven.
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


def test_check_hears_a_written_kick_and_names_an_empty_channel() -> None:
    if not os.environ.get("FL_E2E"):
        pytest.skip("live-FL e2e disabled; set FL_E2E=1 to run")
    if not fl_app().exists():
        pytest.skip("FL Studio not installed")
    if fl_process_running():
        pytest.skip("the live check requires FL Studio closed before it starts")

    source = '''
import json
from fl_mcp import theory

kick = fl.track(0)
kick.write(theory.drums("x...x...x...x...", key="C1"), mode="replace")
for channel in fl.channels[1:]:
    fl.track(channel.index).clear()
result = fl.check(bars=1)
print("CHECK=" + json.dumps(result.model_dump()))
print("PLAYING=" + json.dumps(fl._session.snapshot().playing))
'''
    completed = subprocess.run(
        [sys.executable, "-m", "fl_mcp.dev_session", "--exec", source],
        capture_output=True,
        check=False,
        text=True,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    lines = dict(line.split("=", 1) for line in completed.stdout.splitlines() if line.startswith(("CHECK=", "PLAYING=")))
    result = json.loads(lines["CHECK"])
    first, *rest = result["channels"]
    assert first["sounded"] is True and first["peak_dbfs"] is not None, result
    assert all(c["sounded"] is not True for c in rest if c["mixer_track"] != first["mixer_track"]), result
    assert result["master_peak_dbfs"] is not None, result
    assert json.loads(lines["PLAYING"]) is False, "FL is left stopped"
