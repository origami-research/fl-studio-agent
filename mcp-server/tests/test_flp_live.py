"""Live-FL verification of the v2 writers (G1's live half; THE MANAGER RUNS IT).

Run: FL_E2E=1 pytest tests/test_flp_live.py -m e2e

One FL boot, two crafted projects, all three writers:

* notes_tempo.flp (from Empty): write_notes splice + set_tempo mutate path.
  FL loading the file at all is the load-bearing assertion for the notes
  splice - a malformed event stream is rejected at load (that rejection is
  what killed pyflp.save; docs/21 fact 1) - and the window-title check guards
  against the rejected-file-looks-loaded trap (fact 5).
* levels_tempo.flp (from Basic with limiter): set_channel_levels int32 patches
  (the write-verification addendum A8 says is still owed) + set_tempo append
  path. Levels are read back through FL's own channels API, not our parser.

Byte-diff agreement is not sufficient on its own: the fake always says yes
(AGENTS.md), so every assertion here reads state from the running FL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import flpkit as flp
import pytest

from fl_mcp import wire
from fl_mcp.flpaths import fl_app

pytestmark = pytest.mark.e2e

_EMPTY = "Contents/Resources/FL/Data/Templates/Minimal/Empty/Empty.flp"
# Has channel Levels (219) events and NO tempo event - exercises the levels
# patch and the append-tempo path in one file.
_BASIC = "Contents/Resources/FL/Data/Templates/Minimal/Basic with limiter/Basic with limiter.flp"


def _osa(line: str) -> str:
    return subprocess.run(["osascript", "-e", line], capture_output=True, text=True).stdout.strip()


def _dismiss_fl_modal() -> None:
    if _osa('tell application "System Events" to name of first process whose frontmost is true') == "OsxFL":
        _osa('tell application "System Events" to key code 53')


def _front_window_title() -> str:
    return _osa('tell application "System Events" to tell process "OsxFL" to get name of front window')


def _call(ctl, op: str, *args, budget: float = 90.0):
    """Poll one control-plane op until FL answers or the budget ends. The
    warm-up window after a load parks early calls (addendum A6): a timeout
    here means busy, not absent."""
    deadline = time.time() + budget
    while time.time() < deadline:
        _dismiss_fl_modal()
        try:
            resp = ctl.send_request(op, args=list(args), timeout=3.0)
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
    if not (fl_app() / _EMPTY).is_file():
        pytest.skip("FL Studio (or its stock templates) not installed")

    from harness import flctl

    ctl = wire.MidiControl()  # ports must exist before FL launches
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


def test_v2_writers_land_in_real_fl(own_fl, tmp_path):
    ctl, launch, open_project = own_fl

    # -- file 1: notes splice + mutate-path tempo -----------------------------
    notes_file = tmp_path / "notes_tempo.flp"
    shutil.copy2(fl_app() / _EMPTY, notes_file)
    from fl_mcp.schemas import Note

    written = flp.write_notes(
        notes_file,
        [Note(key=60, start=0.0, length=1.0), Note(key=64, start=1.0, length=1.0),
         Note(key=67, start=2.0, length=2.0)],
        pattern=1, channel=0, mode="replace",
    )
    assert [n.key for n in written] == [60, 64, 67]  # parser readback
    assert flp.set_tempo(notes_file, 137.0) == pytest.approx(137.0)

    launch(notes_file)
    assert notes_file.name in _front_window_title(), "FL rejected the notes splice"
    assert _call(ctl, "mixer.getCurrentTempo") == 137000  # BPM x 1000, from FL

    # -- file 2: channel levels (A8's owed verification) + append-path tempo --
    levels_file = tmp_path / "levels_tempo.flp"
    shutil.copy2(fl_app() / _BASIC, levels_file)
    result = flp.set_channel_levels(levels_file, 2, volume=0.5, pan=0.5, pitch_semitones=-12)
    assert result.volume == pytest.approx(0.5)
    assert flp.set_tempo(levels_file, 91.5) == pytest.approx(91.5)

    open_project(levels_file)
    assert _call(ctl, "mixer.getCurrentTempo") == 91500

    volume = _call(ctl, "channels.getChannelVolume", 2)
    assert volume == pytest.approx(0.5, abs=0.01), (
        f"FL reads channel 2 volume {volume!r}, wrote raw 6400 (= 0.5)"
    )
    pan = _call(ctl, "channels.getChannelPan", 2)
    # FL's API convention for pan needs this run to pin down: -1..1 gives 0.5,
    # 0..1 gives 0.75. Either confirms the write; centre (0.0 / 0.5) or the
    # template's stock value would mean the patch did not land.
    assert pan == pytest.approx(0.5, abs=0.01) or pan == pytest.approx(0.75, abs=0.01), (
        f"FL reads channel 2 pan {pan!r}; wrote raw 9600 (= +0.5 of centre)"
    )
