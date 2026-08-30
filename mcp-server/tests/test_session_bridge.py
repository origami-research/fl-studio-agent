"""The flagged bridge note path: OFF by default, double-gated, readback-only.

Default routing (project-write) is untouched - test_session.py proves that and
stays authoritative. This file proves the gate (env flag AND fresh heartbeat),
the spooled command (ppq stamp included), and that the receipt never trusts
the bridge's echo.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_session import HELLO, NOTES, TARGET, FakeControl, FakeSpool, make_session

from fl_mcp import errors, flp
from fl_mcp import session as session_mod
from fl_mcp.schemas import SpoolResponse
from fl_mcp.session import (
    BRIDGE_HEARTBEAT_FILE,
    FLSession,
    WriteRejected,
    _heartbeat_age,
)


@pytest.fixture()
def bridge_env(tmp_path, monkeypatch):
    """Flag ON, spool rooted at tmp_path, heartbeat fresh; tests degrade it."""
    monkeypatch.setenv("FL_MCP_BRIDGE", "1")
    monkeypatch.setattr(session_mod, "spool_roots", lambda: [tmp_path])
    heartbeat = tmp_path / BRIDGE_HEARTBEAT_FILE
    heartbeat.write_text(
        f"tick=12\napplied=0\npid=1\nunix_time={int(time.time())}\nbackend=bridge\n",
        encoding="utf-8",
    )
    return heartbeat


@pytest.fixture()
def no_reload(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(FLSession, "_reload_fl", lambda self: calls.append(True))
    return calls


def project_flp(monkeypatch, tmp_path, *, ppq=192, file_notes=2):
    """The flp seams the bridge path touches: ppq for the stamp, notes_at for
    the readback. write_notes raises - the bridge path must never file-write."""
    monkeypatch.setattr(flp, "read", lambda path: SimpleNamespace(ppq=ppq))
    monkeypatch.setattr(flp, "notes_at", lambda path, pattern, channel: [object()] * file_notes)

    def never(*a, **k):
        raise AssertionError("the bridge path must not write the project file")

    monkeypatch.setattr(flp, "write_notes", never)
    return tmp_path / "agent.flp"


BRIDGE_OK = SpoolResponse(id=1, ok=True, op="add_notes", notes_written=999)  # 999: a lying echo


# -- the gate -------------------------------------------------------------------


def test_flag_off_routes_project_write_even_with_fresh_heartbeat(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    monkeypatch.delenv("FL_MCP_BRIDGE")
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, _ = make_session(spool=spool, project=tmp_path / "agent.flp")

    assert session.write_notes(TARGET, NOTES, "merge").backend == "project-write"
    assert spool.sent == []


def test_stale_or_missing_heartbeat_routes_project_write(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    stale = int(time.time() - 3600)
    bridge_env.write_text(f"tick=12\nunix_time={stale}\n", encoding="utf-8")
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    spool = FakeSpool()
    session, _ = make_session(spool=spool, project=tmp_path / "agent.flp")

    assert session.write_notes(TARGET, NOTES, "merge").backend == "project-write"
    assert spool.sent == [], "a dead bridge must never receive commands"


def test_heartbeat_age_fails_closed():
    assert _heartbeat_age(Path("/nonexistent/heartbeat.txt")) == math.inf


def test_heartbeat_garbage_fails_closed(tmp_path):
    junk = tmp_path / "hb.txt"
    junk.write_text("unix_time=not-a-number\n", encoding="utf-8")
    assert _heartbeat_age(junk) == math.inf


# -- the bridge write -----------------------------------------------------------


def test_bridge_write_spools_with_ppq_and_reads_back(monkeypatch, tmp_path, bridge_env, no_reload):
    project = project_flp(monkeypatch, tmp_path, ppq=192, file_notes=2)
    spool = FakeSpool(responses={"add_notes": BRIDGE_OK})
    session, store = make_session(spool=spool, project=project)

    receipt = session.write_notes(TARGET, NOTES, "merge")

    assert receipt.backend == "bridge"
    # The spooled command carries the project's ppq; the consumer never guesses.
    assert spool.sent == [
        ("add_notes", {"target": TARGET, "notes": NOTES, "mode": "merge", "ppq": 192})
    ]
    # Readback-only: the count comes from the file read, NEVER the echo (999).
    assert receipt.confirmed_notes == 2
    # Recorded against the spool command id, and FL was never reloaded (the
    # write landed live; a reload would destroy it).
    assert store.writes == [(TARGET, NOTES, "merge", "bridge", 1)]
    assert no_reload == []


def test_bridge_clear_spools_clear_notes(monkeypatch, tmp_path, bridge_env, no_reload):
    project = project_flp(monkeypatch, tmp_path, file_notes=0)
    spool = FakeSpool(
        responses={"clear_notes": SpoolResponse(id=1, ok=True, op="clear_notes", notes_written=0)}
    )
    session, store = make_session(spool=spool, project=project)

    receipt = session.clear_notes(TARGET)

    assert receipt.backend == "bridge" and receipt.confirmed_notes == 0
    assert spool.sent == [("clear_notes", {"target": TARGET})]
    assert store.writes == [(TARGET, [], "replace", "bridge", 1)]
    assert no_reload == []


def test_bridge_refusal_raises_and_records_nothing(monkeypatch, tmp_path, bridge_env, no_reload):
    project = project_flp(monkeypatch, tmp_path)
    spool = FakeSpool(
        responses={"add_notes": SpoolResponse(id=1, ok=False, op="add_notes", err="no plugin host")}
    )
    session, store = make_session(spool=spool, project=project)

    with pytest.raises(WriteRejected, match="no plugin host"):
        session.write_notes(TARGET, NOTES, "merge")
    assert store.writes == []
    assert no_reload == []


def test_bridge_timeout_propagates_note_plane_unavailable(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    project = project_flp(monkeypatch, tmp_path)
    session, store = make_session(spool=FakeSpool(), project=project)  # no scripted response

    with pytest.raises(errors.ToolError) as excinfo:
        session.write_notes(TARGET, NOTES, "merge")
    assert excinfo.value.category == "note_plane_unavailable"
    assert store.writes == []


def test_ppq_stamp_falls_back_to_hello_when_file_unreadable(
    monkeypatch, tmp_path, bridge_env, no_reload
):
    def unreadable(path):
        raise flp.FlpError("torn header")

    monkeypatch.setattr(flp, "read", unreadable)
    monkeypatch.setattr(flp, "notes_at", lambda path, pattern, channel: [])
    spool = FakeSpool(responses={"add_notes": BRIDGE_OK})
    session, _ = make_session(
        control=FakeControl(hello=HELLO), spool=spool, project=tmp_path / "agent.flp"
    )

    session.write_notes(TARGET, NOTES, "merge")

    assert spool.sent[0][1]["ppq"] == HELLO.ppq
