"""Differential test across the Python/C++ seam: a spool written by the REAL
transport-over-file-carrier must be byte-consumable by the REAL bridge_core (via the
bridge_spool_cli shim, which drives bridge_core::PollOnce against the same
mock host as the C++ unit test and prints what FL would have received).

This is the seam where the two languages drift - field names, zero-padding,
nesting, unit conversion - so nothing here is faked on either side.

Skipped (not failed) when the shim is not built; local builds are the gate
(GitHub Actions is dead):

    cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
    cmake --build native/build --target bridge_spool_cli
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from fl_mcp.core.carriers.spool import SpoolCarrier, read_json
from fl_mcp.core.transport import Transport

NPF_EMPTY_FIRST = 1  # fp_plugclass.h

CLI = Path(
    os.environ.get(
        "FLMCP_BRIDGE_CLI",
        Path(__file__).resolve().parents[2] / "native" / "build" / "bridge_spool_cli",
    )
)

pytestmark = pytest.mark.skipif(
    not CLI.is_file(),
    reason="bridge_spool_cli not built; run "
    "`cmake --build native/build --target bridge_spool_cli`",
)

TARGET = {"pattern": 2, "channel": 3}


def drain(home: Path) -> list[dict]:
    result = subprocess.run(
        [str(CLI), "--home", str(home)], capture_output=True, text=True, timeout=30, check=True
    )
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_real_spool_is_consumed_by_bridge_core(tmp_path):
    root = tmp_path / "Documents/Image-Line/FL Studio/Settings/Piano roll scripts/FLMCP"
    spool = Transport([SpoolCarrier(root)], roots=[root])

    # "C4" resolves server-side; the bridge must only ever see the int.
    add = spool.send(
        "add_notes",
        target=TARGET,
        notes=[{"key": "C4", "start": 1, "length": 0.5, "velocity": 1.0, "pan": 1.0}],
        mode="replace",
        ppq=192,
    )
    unstamped = spool.send(
        "add_notes", target={"pattern": 1, "channel": 0}, notes=[{"key": 72, "start": 1, "length": 1}]
    )
    clear = spool.send("clear_notes", target={"pattern": 1, "channel": 0})
    ping = spool.send("ping")

    lines = drain(tmp_path)

    # In send order, each ok, ops intact across the seam.
    assert [(line["id"], line["op"], line["ok"]) for line in lines] == [
        (add.id, "add_notes", True),
        (unstamped.id, "add_notes", True),
        (clear.id, "clear_notes", True),
        (ping.id, "ping", True),
    ]

    # The stamped command: target honored, ppq=192 drives the tick conversion,
    # replace maps to NPF_EmptyFirst, levels convert to FL units.
    (chord,) = lines[0]["chords"]
    assert (chord["pat_num"], chord["chan_num"]) == (2, 3)
    assert chord["flags"] == NPF_EMPTY_FIRST
    (note,) = chord["notes"]
    assert note["note"] == 60
    assert note["position"] == 192  # 1 beat at the stamped ppq
    assert note["length"] == 96  # 0.5 beats
    # Vol is a Single on the same 0..1 scale as the wire velocity, not an
    # int 0..128: FLEngine's own record RTTI types TNoteParams @12 as Single.
    assert note["vol"] == 1.0  # velocity 1.0
    assert note["pan"] == 64  # pan 1.0 -> +64 (centre 0)

    # No ppq stamp: the bridge's last-resort default (96) places the note.
    (chord,) = lines[1]["chords"]
    assert chord["notes"][0]["position"] == 96
    assert chord["flags"] == 0  # default mode is merge

    # clear_notes: empty-first with zero notes, target intact.
    (chord,) = lines[2]["chords"]
    assert (chord["count"], chord["flags"], chord["pat_num"]) == (0, NPF_EMPTY_FIRST, 1)

    # ping never reaches the host.
    assert lines[3]["chords"] == []

    # The server consumes the bridge's responses through the SAME code path the
    # session uses - proto shape verified by pydantic, not by string matching.
    for cmd in (add, unstamped, clear, ping):
        resp = spool.await_response(cmd, timeout=1.0)
        assert resp.ok is True and resp.id == cmd.id

    # Exactly-once: ledger holds every id, commands are tombstoned, and a
    # second drain finds nothing.
    assert read_json(root / "consumed.json")["ids"] == [add.id, unstamped.id, clear.id, ping.id]
    assert read_json(root / "commands" / f"{add.id:06d}.json") == {"consumed": True, "id": add.id}
    assert drain(tmp_path) == []


def test_cancelled_command_is_never_applied(tmp_path):
    """A server-side timeout tombstones the command; the bridge must skip it
    without claiming it, exactly like the pyscript."""
    root = tmp_path / "Documents/Image-Line/FL Studio/Settings/Piano roll scripts/FLMCP"
    spool = Transport([SpoolCarrier(root)], roots=[root])
    cmd = spool.send("ping")
    with pytest.raises(Exception, match="cancelled"):
        spool.await_response(cmd, timeout=0.01)

    assert drain(tmp_path) == []
    assert read_json(root / "consumed.json") is None
