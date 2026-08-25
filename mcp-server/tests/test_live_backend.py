"""Prove the live backend is wired correctly, without the macOS grant.

The one part that cannot be tested here is macOS actually delivering the
keystroke — that needs a human-granted permission. Everything else is testable
by standing in for the trigger: backend precedence, the spool round trip, what
happens when the in-FL script answers, and what happens when it does not.

This exists so that when the user finally grants permission, the surrounding
logic is already known-good and any failure is unambiguously the grant.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from fl_mcp import live, server, spool  # noqa: E402


@pytest.fixture(autouse=True)
def fl_home(tmp_path, monkeypatch):
    settings = tmp_path / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    (settings / "Piano roll scripts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Keep the project-write backend out of the way; it is a separate path.
    monkeypatch.setattr(server, "_try_project_write", lambda notes, mode: None)
    return tmp_path


class FakeTrigger:
    """Stands in for macOS keystroke delivery.

    `applied` mimics the in-FL script: on trigger, write a result for whatever
    command is pending, exactly as the real .pyscript does.
    """

    def __init__(self, *, permitted=True, fl_running=True, script_responds=True):
        self.permitted = permitted
        self.fl_running_flag = fl_running
        self.script_responds = script_responds
        self.presses = 0

    def is_fl_running(self):
        return self.fl_running_flag

    def check_permission(self):
        # Defined locally, not imported: this object *is* harness.trigger for
        # the duration of the test, so importing from it would recurse.
        return SimpleNamespace(granted=self.permitted, detail="fake")

    def run_piano_roll_script(self):
        self.presses += 1
        if not self.script_responds:
            return
        pending = spool.pending_command()
        if pending is None:
            return
        for root in spool.spool_roots():
            spool.write_json_atomic(
                root / "result.json",
                {
                    "id": pending["id"],
                    "ok": True,
                    "op": pending.get("op"),
                    "summary": "applied in place",
                    "note_count": len(pending.get("notes", [])),
                },
            )


def install(monkeypatch, fake: FakeTrigger, hotkey_bound=True, tmp_path=None):
    monkeypatch.setitem(sys.modules, "harness.trigger", fake)
    marker = (tmp_path or Path("/tmp")) / ".hotkey_bound"
    if hotkey_bound:
        marker.write_text("verified")
    monkeypatch.setattr(live, "HOTKEY_MARKER", marker)
    return fake


NOTES = [{"key": "C4", "start": 0, "length": 1}, {"key": "E4", "start": 1, "length": 1}]


def _shorten_live_timeout(monkeypatch, seconds: float = 1.0):
    """Keep no-response tests from waiting out the real 15s timeout."""
    real_apply = live.apply
    monkeypatch.setattr(
        live, "apply", lambda cid, timeout=seconds: real_apply(cid, timeout=seconds)
    )


def test_live_backend_applies_without_reloading_fl(monkeypatch, fl_home):
    fake = install(monkeypatch, FakeTrigger(), tmp_path=fl_home)

    result = server.write_notes(NOTES)

    assert result["status"] == "applied"
    assert result["backend"] == "live", "live must win when it is usable"
    assert result["notes_in_piano_roll"] == 2
    assert fake.presses == 1, "should press the shortcut exactly once"
    assert "reloaded" not in result.get("note", "").lower() or "not reloaded" in result["note"]


def test_live_is_preferred_over_project_write(monkeypatch, fl_home):
    """Both available -> live wins, because it keeps the user's session."""
    install(monkeypatch, FakeTrigger(), tmp_path=fl_home)
    monkeypatch.setattr(
        server,
        "_try_project_write",
        lambda notes, mode: {
            "backend": "project-write",
            "project": "x",
            "notes_in_pattern": 99,
        },
    )

    result = server.write_notes(NOTES)
    assert result["backend"] == "live"


@pytest.mark.parametrize(
    ("kwargs", "hotkey", "expected_blocker"),
    [
        ({"permitted": False}, True, "automation"),
        ({}, False, "shortcut"),
        ({"fl_running": False}, True, "not running"),
    ],
)
def test_unavailable_reasons_are_named(monkeypatch, fl_home, kwargs, hotkey, expected_blocker):
    """An unavailable backend must say which prerequisite is missing."""
    install(monkeypatch, FakeTrigger(**kwargs), hotkey_bound=hotkey, tmp_path=fl_home)

    result = server.write_notes(NOTES)

    assert result["status"] == "queued"
    blockers = " ".join(result["live_backend_blocked_by"]).lower()
    assert expected_blocker in blockers, blockers


def test_no_response_from_script_does_not_claim_success(monkeypatch, fl_home):
    """Permission granted but FL ignores the key: must not report an edit."""
    fake = install(monkeypatch, FakeTrigger(script_responds=False), tmp_path=fl_home)
    _shorten_live_timeout(monkeypatch)

    result = server.write_notes(NOTES)

    assert result["status"] == "queued", "an unanswered trigger is not an applied edit"
    assert fake.presses == 1


def test_command_reaches_the_spool_for_the_script_to_find(monkeypatch, fl_home):
    install(monkeypatch, FakeTrigger(script_responds=False), tmp_path=fl_home)
    _shorten_live_timeout(monkeypatch)

    server.write_notes(NOTES)

    pending = spool.pending_command()
    assert pending is not None
    assert pending["op"] == "add_notes"
    assert len(pending["notes"]) == 2
    assert pending["notes"][0]["key"] == 60
