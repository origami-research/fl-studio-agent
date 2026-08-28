"""Live-FL verification of the FILE-write backend's outputs.

The fast suite proves our own parser accepts what flp_writer produces; these
tests prove REAL FL accepts it - the two are independent claims, and only the
second one matters to a user. This distinction is not hypothetical: pyflp's
serializer corrupted the 'Basic with limiter' template (wrong FLhd channel
count + a mangled text event) in ways our parser read back happily while real
FL refused to load the file at all - which is what forced set_tempo onto raw
byte surgery (see flp_writer.set_tempo). Boots its own FL on crafted projects,
so it does not share the session-scoped ``live_fl`` fixture.

Run: FL_E2E=1 pytest tests/e2e/test_flp_writer_live.py -m e2e
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from fl_mcp import control_midi
from fl_mcp.flp_writer import set_tempo
from fl_mcp.flpaths import fl_app

pytestmark = pytest.mark.e2e

# A template FL ships WITHOUT a tempo event (default tempo is expressed by
# omission), so it exercises the appended-record path end to end.
_NO_TEMPO_TEMPLATE = (
    "Contents/Resources/FL/Data/Templates/Minimal/Basic with limiter/Basic with limiter.flp"
)
# A template WITH a native tempo event (128 BPM), exercising the mutate path.
_HAS_TEMPO_TEMPLATE = (
    "Contents/Resources/FL/Data/Templates/Utility/Create a chord progression/Create a chord progression.flp"
)


def _osa(line: str) -> str:
    return subprocess.run(["osascript", "-e", line], capture_output=True, text=True).stdout.strip()


def _dismiss_fl_modal() -> None:
    """Escape any FL dialog (welcome screen etc.) - modals freeze OnIdle, which
    parks every FL-API request forever (ping still answers; see docs/14)."""
    if _osa('tell application "System Events" to name of first process whose frontmost is true') == "OsxFL":
        _osa('tell application "System Events" to key code 53')


def _front_window_title() -> str:
    return _osa('tell application "System Events" to tell process "OsxFL" to get name of front window')


def _read_tempo(ctl, budget: float = 90.0):
    """Poll getCurrentTempo with modal dismissal until FL answers or budget ends."""
    deadline = time.time() + budget
    while time.time() < deadline:
        _dismiss_fl_modal()
        try:
            resp = ctl.send_request("mixer.getCurrentTempo", timeout=3.0)
        except Exception:
            time.sleep(1)
            continue
        if resp.ok:
            return resp.ret
        time.sleep(1)
    return None


@pytest.fixture
def own_fl(tmp_path):
    """Ports first, then FL under this fixture's control; SIGKILL teardown."""
    if not os.environ.get("FL_E2E"):
        pytest.skip("live-FL e2e disabled; set FL_E2E=1 to run")
    if not (fl_app() / _NO_TEMPO_TEMPLATE).is_file():
        pytest.skip("FL Studio (or its stock templates) not installed")

    from harness import flctl

    ctl = control_midi.MidiControl()  # ports must exist before FL launches
    if flctl.is_running():
        flctl.stop(force=True)
        time.sleep(1)

    def launch(path):
        flctl.start(project=path)
        deadline = time.time() + 150
        while time.time() < deadline:
            for ev in ctl.drain_events():
                if ev.get("op") == "hello":
                    return
            _dismiss_fl_modal()
            time.sleep(1)
        pytest.fail("controller never said hello within 150s")

    def open_project(path):
        """Open another project in the already-running FL (no re-boot)."""
        subprocess.run(["open", "-a", str(fl_app()), str(path)], check=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            _dismiss_fl_modal()
            if path.name in _front_window_title():
                return
            time.sleep(1)
        pytest.fail(f"FL never loaded {path.name} - the file may be rejected")

    yield ctl, launch, open_project

    flctl.stop(force=True)
    ctl.close()


def test_set_tempo_files_load_and_read_back_in_real_fl(own_fl, tmp_path):
    """Both set_tempo paths (append on a no-tempo-event file, mutate on a
    native one), verified by FL's own loader and getCurrentTempo - one boot.

    The title assertions matter as much as the tempo: a rejected file leaves
    FL on an empty default project whose tempo read-back can look plausible.
    """
    ctl, launch, open_project = own_fl

    appended = tmp_path / "tempo_appended.flp"
    shutil.copy2(fl_app() / _NO_TEMPO_TEMPLATE, appended)
    assert set_tempo(appended, 137.0) == pytest.approx(137.0)  # parser readback

    mutated = tmp_path / "tempo_mutated.flp"
    shutil.copy2(fl_app() / _HAS_TEMPO_TEMPLATE, mutated)
    assert set_tempo(mutated, 91.5) == pytest.approx(91.5)

    launch(appended)
    assert appended.name in _front_window_title(), "FL rejected the appended-tempo file"
    assert _read_tempo(ctl) == 137000  # BPM x 1000, from FL itself

    open_project(mutated)
    assert _read_tempo(ctl) == 91500
