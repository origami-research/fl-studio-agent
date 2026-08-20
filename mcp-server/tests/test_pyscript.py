"""Run the real .pyscript under an emulation of FL Studio's sandbox.

Every test here goes through `fl_env.run_pyscript`, which reproduces FL's
restricted `open`, its blocked `os.remove`, its `SystemError` (not `OSError`)
failure mode, and its habit of calling `apply()` with no arguments. Tests that
bypass that sandbox have already proven able to pass while the real thing fails.

Only FL's own note-writing behavior remains unverified here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fake_flpianoroll
import fl_env
from fl_mcp import spool
from fl_mcp.server import clear_notes, fl_status, write_notes

SCRIPT = (
    Path(__file__).resolve().parents[2] / "fl-scripts" / "piano-roll" / "MCP Write Notes.pyscript"
)


@pytest.fixture(autouse=True)
def fl_home(tmp_path, monkeypatch):
    """A fake FL install that both the server and the script discover on their own."""
    settings = tmp_path / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    (settings / "Piano roll scripts").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    fake_flpianoroll.reset()
    return settings


def run_script(fl_home_dir: Path | None = None, allowed: list[Path] | None = None):
    """Run the script as FL would. `allowed` overrides the readable roots."""
    roots = allowed if allowed is not None else [fl_home_dir or Path.home()]
    return fl_env.run_pyscript(SCRIPT, roots)


def last_message() -> str:
    return fake_flpianoroll.Utils.messages[-1] if fake_flpianoroll.Utils.messages else ""


# --- layout agreement ------------------------------------------------------


def test_server_and_script_agree_on_spool_location(fl_home):
    module = run_script(fl_home)
    script_roots = [Path(p) for p in module._spool_roots()]
    for root in spool.spool_roots():
        assert root in script_roots, f"server writes to {root}, script never looks there"


def test_spool_lives_inside_fl_settings(fl_home):
    """FL's restricted open() cannot read outside its own tree."""
    for root in spool.spool_roots():
        assert fl_home in root.parents or root.parent == fl_home


# --- the happy path --------------------------------------------------------


def test_write_notes_lands_in_piano_roll(fl_home):
    result = write_notes(
        [
            {"key": "C4", "start": 0, "length": 1},
            {"key": "E4", "start": 1, "length": 0.5, "velocity": 1.0},
            {"key": 67, "start": 2, "length": 2, "pan": 0.25, "color": 3},
        ]
    )
    assert result["status"] == "queued"
    assert fake_flpianoroll.score.noteCount == 0, "must not change FL before the script runs"

    run_script(fl_home)

    notes = fake_flpianoroll.score._notes
    assert len(notes) == 3, last_message()
    # beats -> ticks at PPQ 96
    assert (notes[0].number, notes[0].time, notes[0].length) == (60, 0, 96)
    assert (notes[1].number, notes[1].time, notes[1].length) == (64, 96, 48)
    assert (notes[2].number, notes[2].time, notes[2].length) == (67, 192, 192)
    assert notes[1].velocity == 1.0
    assert notes[2].pan == 0.25
    assert notes[2].color == 3
    assert notes[0].velocity == 0.8
    assert notes[0].pan == 0.5


def test_ppq_is_read_from_the_score(fl_home):
    fake_flpianoroll.reset(ppq=960)
    write_notes([{"key": "C4", "start": 1, "length": 1}])
    run_script(fl_home)

    note = fake_flpianoroll.score._notes[0]
    assert (note.time, note.length) == (960, 960)


def test_merge_keeps_existing_replace_clears(fl_home):
    write_notes([{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)
    write_notes([{"key": "D4", "start": 1, "length": 1}], mode="merge")
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 2, last_message()

    write_notes([{"key": "G4", "start": 0, "length": 1}], mode="replace")
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1
    assert fake_flpianoroll.score._notes[0].number == 67


def test_clear_notes(fl_home):
    write_notes([{"key": "C4", "start": 0, "length": 1}, {"key": "E4", "start": 0, "length": 1}])
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 2

    clear_notes()
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 0
    assert spool.read_result()["summary"] == "cleared 2 note(s)"


def test_status_reports_applied_result(fl_home):
    write_notes([{"key": "C4", "start": 0, "length": 1}])
    assert fl_status()["pending_command"]["op"] == "add_notes"

    run_script(fl_home)

    done = fl_status()
    assert done["pending_command"] is None, "tombstone must not read as pending"
    assert done["last_result"]["ok"] is True
    assert done["last_result"]["notes_in_piano_roll"] == 1


# --- the sandbox regressions ----------------------------------------------


def test_command_applies_exactly_once(fl_home):
    """os.remove is blocked, so consumption relies on tombstones working."""
    assert len(spool.spool_roots()) > 1, "test is meaningless with a single root"

    write_notes([{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, last_message()

    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, "command must not re-apply"
    assert "nothing to apply" in last_message().lower()


def test_apply_is_callable_with_no_arguments(fl_home):
    """FL calls apply() bare when there is no createDialog(); apply(form) raised TypeError."""
    module = run_script(fl_home)
    module.apply()  # must not raise
    assert module.apply.__defaults__ == (None,)


def test_blocked_os_remove_does_not_break_the_run(fl_home):
    """The original failure: SystemError from os.remove escaped an `except OSError`."""
    write_notes([{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 1, last_message()
    assert "unexpected error" not in last_message().lower()
    assert spool.read_result()["ok"] is True


def _block_writes(monkeypatch, under: Path | None = None):
    """Make open(...,'w') raise SystemError, as FL does for non-writable folders."""
    real = fl_env.sandboxed_open

    def factory(allowed_roots):
        guarded = real(allowed_roots)

        def guarded_write(file, mode="r", *args, **kwargs):
            blocked_here = under is None or str(under) in str(file)
            if "w" in mode and blocked_here:
                raise SystemError(
                    "<class '_io.FileIO'> returned NULL without setting an exception"
                )
            return guarded(file, mode, *args, **kwargs)

        return guarded_write

    monkeypatch.setattr(fl_env, "sandboxed_open", factory)


def test_consumption_survives_a_read_only_spool_folder(fl_home, monkeypatch):
    """The real failure: the folder holding the command is readable, not writable.

    Consumption must fall back to a ledger in some other writable root rather
    than silently re-applying the command.
    """
    write_notes([{"key": "C4", "start": 0, "length": 1}])
    _block_writes(monkeypatch, under=fl_home / "Piano roll scripts" / "FLMCP")

    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, last_message()
    assert spool.read_result()["consumed"] is True
    assert "second time" not in last_message()

    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, "must not re-apply from the read-only root"


def test_fully_unwritable_spool_warns_instead_of_silently_duplicating(fl_home, monkeypatch):
    """If nothing anywhere is writable the command will re-apply; say so."""
    write_notes([{"key": "C4", "start": 0, "length": 1}])
    _block_writes(monkeypatch)

    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 1, "the edit itself should still apply"
    assert spool.read_result() is None, "result cannot be written either"
    assert "second time" in last_message()


def test_unreadable_spool_is_diagnosed(fl_home):
    """File visible but open() refused — must name the path, not say 'nothing to apply'."""
    write_notes([{"key": "C4", "start": 0, "length": 1}])

    # Nothing is readable: emulates the spool sitting outside FL's tree.
    run_script(allowed=[fl_home / "nowhere"])

    assert fake_flpianoroll.score.noteCount == 0
    msg = last_message()
    assert "could not read it" in msg
    assert "pending.json" in msg, "must name the path so the user can act on it"


def test_encoding_kwarg_is_never_used(fl_home):
    """FL's open() rejects encoding=; the sandbox raises TypeError if we pass it."""
    write_notes([{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, last_message()


# --- protocol / bad input --------------------------------------------------


def test_protocol_mismatch_is_reported(fl_home):
    for root in spool.spool_roots():
        spool.write_json_atomic(
            root / "pending.json", {"proto": 99, "id": 1, "op": "add_notes", "notes": []}
        )
    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 0
    assert spool.read_result()["ok"] is False
    assert "protocol mismatch" in spool.read_result()["error"]


def test_malformed_notes_do_not_crash_fl(fl_home):
    for root in spool.spool_roots():
        spool.write_json_atomic(
            root / "pending.json",
            {"proto": 1, "id": 7, "op": "add_notes", "notes": [{"key": "not-a-note"}]},
        )
    run_script(fl_home)

    result = spool.read_result()
    assert result["ok"] is False and result["id"] == 7
    assert fake_flpianoroll.Utils.messages, "user must be told it failed"
