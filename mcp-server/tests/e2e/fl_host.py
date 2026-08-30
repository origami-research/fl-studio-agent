"""THE live-FL host harness - the one canonical launch/attach/readback path.

Every live e2e drives FL through this class, so the hard-won rules live in
exactly one place:

* ports before launch, with hot-plug self-heal left to the wire (docs/21
  addendum 10 fact 4);
* modal dismissal while waiting (fact 6: OnIdle freezes under a modal, so a
  stuck dialog silences the controller);
* the window-title positive control sampled across EVERY window (fact 5 +
  addendum A14: a rejected file leaves FL on a default project whose
  readbacks look plausible, and the front window can be an untitled child);
* poll-until-answered calls (addendum A6: the first engine call after a load
  parks as "unsafe at current time" - a timeout means busy, not absent).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from harness import flctl

from fl_mcp import wire
from fl_mcp.flpaths import fl_app


def _osa(line: str) -> str:
    return subprocess.run(
        ["osascript", "-e", line], capture_output=True, text=True
    ).stdout.strip()


class FLHost:
    """Owns the MIDI ports and the FL process for one live-test module."""

    def __init__(self) -> None:
        self.ctl = wire.MidiControl()  # ports must exist before FL launches
        self.hello: dict | None = None
        if flctl.is_running():
            flctl.stop(force=True)
            time.sleep(1)

    def dismiss_modal(self) -> None:
        """Escape any FL dialog, but only when FL is frontmost."""
        frontmost = _osa(
            'tell application "System Events" to name of first process whose frontmost is true'
        )
        if frontmost == "OsxFL":
            _osa('tell application "System Events" to key code 53')

    def window_titles(self) -> str:
        """Every FL window title, one string (A14: sample all windows)."""
        return _osa(
            'tell application "System Events" to tell process "OsxFL" to get name of every window'
        )

    def launch(self, project: Path | None = None, timeout: float = 150.0) -> dict:
        """Start FL (optionally on a project) and wait for the controller's hello."""
        flctl.start(project=project)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for event in self.ctl.drain_events():
                if event.get("op") == "hello":
                    self.hello = event
                    return event
            self.dismiss_modal()
            time.sleep(1)
        pytest.fail(f"controller never said hello within {timeout:g}s")

    def open(self, path: Path, timeout: float = 120.0) -> None:
        """Open a project in the running FL and wait until a window title
        proves FL loaded THAT file (the positive control of fact 5)."""
        subprocess.run(["open", "-a", str(fl_app()), str(path)], check=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.dismiss_modal()
            if path.name in self.window_titles():
                return
            time.sleep(1)
        pytest.fail(
            f"FL never showed {path.name} in any window title within {timeout:g}s - "
            f"the file may be rejected; titles now: {self.window_titles()!r}"
        )

    def call(self, op: str, *args, budget: float = 90.0):
        """Poll one control-plane op until FL answers or the budget ends
        (the settle rule: post-load timeouts mean busy, not absent)."""
        deadline = time.time() + budget
        while time.time() < deadline:
            self.dismiss_modal()
            try:
                resp = self.ctl.send_request(op, args=list(args), timeout=3.0)
            except Exception:
                time.sleep(1)
                continue
            if resp.ok:
                return resp.ret
            time.sleep(1)
        pytest.fail(f"{op}{args} never answered ok within {budget:g}s")

    def close(self) -> None:
        flctl.stop(force=True)
        self.ctl.close()
