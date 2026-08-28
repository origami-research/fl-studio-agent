"""Run the real .pyscript under an emulation of FL Studio's sandbox.

Every test here goes through `fl_env.run_pyscript`, which reproduces FL's
restricted `open`, its blocked `os.remove`, its `SystemError` (not `OSError`)
failure mode, and its habit of calling `apply()` with no arguments. Tests that
bypass that sandbox have already proven able to pass while the real thing fails.

The server side is the v2 spool (fl_mcp.spool.Spool) - the ONE sequenced
protocol. Only FL's own note-writing behavior remains unverified here.
"""

from __future__ import annotations

from pathlib import Path

import fake_flpianoroll
import fl_env
import pytest

from fl_mcp.errors import ToolError
from fl_mcp.spool import Spool, read_json, spool_roots, write_json_atomic

SCRIPT = (
    Path(__file__).resolve().parents[2] / "fl-scripts" / "piano-roll" / "MCP Write Notes.pyscript"
)

TARGET = {"pattern": 1, "channel": 0}


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


@pytest.fixture()
def spool():
    """Discovery-mode spool, exactly as create_session builds it."""
    return Spool()


def write_notes(spool: Spool, notes: list[dict], mode: str = "merge"):
    return spool.send("add_notes", target=TARGET, notes=notes, mode=mode)


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
    for root in spool_roots():
        assert root in script_roots, f"server writes to {root}, script never looks there"


def test_spool_lives_inside_fl_settings(fl_home):
    """FL's restricted open() cannot read outside its own tree."""
    for root in spool_roots():
        assert fl_home in root.parents or root.parent == fl_home


def test_server_and_script_agree_on_protocol_version(fl_home, spool):
    cmd = spool.send("ping")
    module = run_script(fl_home)
    assert cmd.proto == module.PROTO_VER


# --- the happy path --------------------------------------------------------


def test_write_notes_lands_in_piano_roll(fl_home, spool):
    cmd = write_notes(
        spool,
        [
            {"key": "C4", "start": 0, "length": 1},
            {"key": "E4", "start": 1, "length": 0.5, "velocity": 1.0},
            {"key": 67, "start": 2, "length": 2, "pan": 0.25, "color": 3},
        ],
    )
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

    # The response is the READBACK: notes in the piano roll after the command.
    resp = spool.await_response(cmd, timeout=1.0)
    assert resp.ok is True
    assert resp.op == "add_notes"
    assert resp.notes_written == 3


def test_ppq_is_read_from_the_score(fl_home, spool):
    fake_flpianoroll.reset(ppq=960)
    write_notes(spool, [{"key": "C4", "start": 1, "length": 1}])
    run_script(fl_home)

    note = fake_flpianoroll.score._notes[0]
    assert (note.time, note.length) == (960, 960)


def test_merge_keeps_existing_replace_clears(fl_home, spool):
    write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)
    write_notes(spool, [{"key": "D4", "start": 1, "length": 1}], mode="merge")
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 2, last_message()

    write_notes(spool, [{"key": "G4", "start": 0, "length": 1}], mode="replace")
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1
    assert fake_flpianoroll.score._notes[0].number == 67


def test_clear_notes(fl_home, spool):
    write_notes(
        spool,
        [{"key": "C4", "start": 0, "length": 1}, {"key": "E4", "start": 0, "length": 1}],
    )
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 2

    cmd = spool.send("clear_notes", target=TARGET)
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 0
    resp = spool.await_response(cmd, timeout=1.0)
    assert resp.ok is True
    assert resp.notes_written == 0


def test_queued_commands_apply_in_order_in_one_run(fl_home, spool):
    """The sequenced protocol's point: several pending commands, one trigger."""
    first = write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    second = write_notes(spool, [{"key": "G4", "start": 0, "length": 1}], mode="replace")
    third = write_notes(spool, [{"key": "E4", "start": 1, "length": 1}])

    run_script(fl_home)

    # replace ran AFTER the first add (order matters), then the merge landed.
    assert [n.number for n in fake_flpianoroll.score._notes] == [67, 64], last_message()
    for cmd, expected in ((first, 1), (second, 1), (third, 2)):
        resp = spool.await_response(cmd, timeout=1.0)
        assert resp.ok is True
        assert resp.notes_written == expected


def test_cancelled_command_never_applies(fl_home, spool):
    """await_response timeout tombstones the command; a late trigger must skip it."""
    cmd = write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    with pytest.raises(ToolError):
        spool.await_response(cmd, timeout=0.05)

    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 0, "cancelled command must not apply"
    assert "nothing to apply" in last_message().lower()


# --- the sandbox regressions ----------------------------------------------


def test_command_applies_exactly_once(fl_home, spool):
    """os.remove is blocked, so consumption relies on tombstones + the ledger."""
    assert len(spool_roots()) > 1, "test is meaningless with a single root"

    write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
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


def test_blocked_os_remove_does_not_break_the_run(fl_home, spool):
    """The original failure: SystemError from os.remove escaped an `except OSError`."""
    cmd = write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 1, last_message()
    assert "unexpected error" not in last_message().lower()
    assert spool.await_response(cmd, timeout=1.0).ok is True


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


def test_consumption_survives_a_read_only_spool_folder(fl_home, spool, monkeypatch):
    """The real failure: the folder holding the command is readable, not writable.

    Consumption must fall back to a ledger in some other writable root rather
    than silently re-applying the command.
    """
    cmd = write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    _block_writes(monkeypatch, under=fl_home / "Piano roll scripts" / "FLMCP")

    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, last_message()
    assert spool.await_response(cmd, timeout=1.0).ok is True
    assert "second time" not in last_message()

    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, "must not re-apply from the read-only root"


def test_fully_unwritable_spool_warns_instead_of_silently_duplicating(fl_home, spool, monkeypatch):
    """If nothing anywhere is writable the command will re-apply; say so."""
    cmd = write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    _block_writes(monkeypatch)

    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 1, "the edit itself should still apply"
    for root in spool_roots():
        assert read_json(root / "responses" / f"{cmd.id:06d}.json") is None, (
            "response cannot be written either"
        )
    assert "second time" in last_message()


def test_unreadable_spool_is_diagnosed(fl_home, spool):
    """File visible but open() refused — must name the path, not say 'nothing to apply'."""
    write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])

    # Nothing is readable: emulates the spool sitting outside FL's tree.
    run_script(allowed=[fl_home / "nowhere"])

    assert fake_flpianoroll.score.noteCount == 0
    msg = last_message()
    assert "could not read" in msg
    assert "queue.json" in msg, "must name the path so the user can act on it"


def test_encoding_kwarg_is_never_used(fl_home, spool):
    """FL's open() rejects encoding=; the sandbox raises TypeError if we pass it."""
    write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1, last_message()


# --- protocol / bad input --------------------------------------------------


def _handcraft_command(payload: dict) -> None:
    """Plant a raw command + queue entry in every root, bypassing send()'s
    validation - the only way to exercise the script against bad input."""
    for root in spool_roots():
        write_json_atomic(root / "commands" / f"{payload['id']:06d}.json", payload)
        write_json_atomic(root / "commands" / "queue.json", {"proto": 2, "ids": [payload["id"]]})


def test_protocol_mismatch_is_reported(fl_home, spool):
    _handcraft_command({"proto": 99, "id": 1, "op": "add_notes", "args": {"notes": []}})
    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 0
    resp = read_json(spool_roots()[0] / "responses" / "000001.json")
    assert resp["ok"] is False
    assert "protocol mismatch" in resp["err"]

    # Consumed despite the mismatch: a bad command must not block the queue.
    run_script(fl_home)
    assert "nothing to apply" in last_message().lower()


def test_malformed_notes_do_not_crash_fl(fl_home, spool):
    _handcraft_command(
        {"proto": 2, "id": 7, "op": "add_notes", "args": {"notes": [{"key": "not-a-note"}]}}
    )
    run_script(fl_home)

    resp = read_json(spool_roots()[0] / "responses" / "000007.json")
    assert resp["ok"] is False and resp["id"] == 7
    assert fake_flpianoroll.Utils.messages, "user must be told it failed"


def test_corrupt_ledger_refuses_instead_of_replaying(fl_home, spool):
    """A torn/corrupt consumed.json is the one thing standing between an
    already-applied command and a silent replay - the script must fail closed."""
    write_notes(spool, [{"key": "C4", "start": 0, "length": 1}])
    run_script(fl_home)
    assert fake_flpianoroll.score.noteCount == 1

    # Corrupt every ledger the script can see (torn write simulation), then
    # queue a new command. With the ledger unreadable, applying could replay -
    # the script must refuse and say so, not guess "nothing applied yet".
    for root in spool_roots():
        ledger = root / "consumed.json"
        if ledger.exists():
            ledger.write_text("{not json", encoding="utf-8")

    write_notes(spool, [{"key": "E4", "start": 1, "length": 1}])
    run_script(fl_home)

    assert fake_flpianoroll.score.noteCount == 1  # nothing was applied
    assert "refusing" in last_message().lower()
