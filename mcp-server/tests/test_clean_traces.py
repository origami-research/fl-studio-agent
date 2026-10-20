"""Clean traces: what a model sees while it writes and reads on a trial FL.

Live runs bdea5e90 and b5343990 (2026-10-18, FL Trial 26.1.6, session mode)
lost 5-8 of ~25 steps doubting the engine. Every write came back
``unconfirmed_write=True`` (a trial cannot save, so the save-and-reparse
readback never ran), reads came from the stale project file (tempo 130, no
notes after set_tempo(100) and 16 kick notes), and results spoke of bridges,
backends and receipts. The models slept, re-read, called fl.save() and
fl_doctor, and read docstrings.

This replays those sequences through the shipped path - the real FLSession,
note lanes, store, MCP tools and a real kernel process - with only FL itself
faked (its controller answers and the Bridge's acknowledgements), and holds
the line: a write is definitive, an immediate read returns it, the scene shows
the live tempo, and no model-facing payload names the engine's internals. The
second half scans every model-facing string the engine can produce.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult
from pydantic import BaseModel
from session_fakes import HELLO, FakeControl, FakeSpool, make_session

from fl_mcp import schemas, sdk, server
from fl_mcp.contract import FLLicense, build_contract
from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import PosixLauncher, Workspace
from fl_mcp.schemas import Note, SpoolResponse, Target
from fl_mcp.session import detect as detect_mod
from fl_mcp.session import facade as facade_mod
from fl_mcp.session.detect import BRIDGE_HEARTBEAT_FILE
from fl_mcp.store import Store
from fl_mcp.template import template_path

# The engine's internals, as the models in those runs quoted them back.
INTERNAL_WORDS = re.compile(
    r"bridge|\blanes?\b|backend|spool|receipt|unconfirmed|provenance|shadow|project[ _-]?file|\.flp\b|fl_doctor",
    re.IGNORECASE,
)
TEMPLATE_CHANNELS = ["808 Kick", "808 Clap", "808 HiHat", "808 Snare", "FLEX Bass"]
LIVE_PROFILE = SimpleNamespace(capabilities=SimpleNamespace(channel_targeting_live=True))
ACK = {
    "ping": SpoolResponse(id=0, ok=True, op="ping"),
    "add_notes": SpoolResponse(id=0, ok=True, op="add_notes", notes_written=999),  # an echo, never trusted
    "clear_notes": SpoolResponse(id=0, ok=True, op="clear_notes"),
}


def trial_fl() -> FakeControl:
    """FL's controller as a trial box answers it: live tempo and channel selection."""
    state = {"tempo": 130_000, "selected": 0}
    snapshot = lambda args: {  # noqa: E731 - one expression, read in place
        "tempo": state["tempo"],
        "ppq": 96,
        "playing": False,
        "current_pattern": 1,
        "mixer": [],
        "channels": [
            {"idx": i, "name": name, "vol": 0.78, "pan": 0.0, "muted": False}
            for i, name in enumerate(TEMPLATE_CHANNELS)
        ],
    }
    return FakeControl(
        hello=HELLO,
        handlers={
            "mixer.setCurrentTempo": lambda args: state.update(tempo=round(args[0] * 1000)),
            "mixer.getCurrentTempo": lambda args: state["tempo"],
            "channels.selectedChannel": lambda args: state["selected"],
            "select_channels": lambda args: state.update(selected=args[0][0]),
            "state.snapshot": snapshot,
        },
    )


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A trial session in session mode on a fresh template, with a live Bridge."""
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [tmp_path])
    monkeypatch.setattr(facade_mod, "profile_gate", lambda: (True, "FL Trial v26.1.6 [build 5639]"))
    (tmp_path / BRIDGE_HEARTBEAT_FILE).write_text(
        f"tick=1\napplied=0\npid=1\nunix_time={int(time.time())}\nproto=2\nabi=ok\npat=-1\nchan=-1\n",
        encoding="utf-8",
    )
    project = tmp_path / "FLMCP Agent.flp"
    shutil.copyfile(template_path(), project)
    control = trial_fl()
    session, _ = make_session(
        control=control,
        transport=FakeSpool(responses=dict(ACK)),
        store=Store("trial", root=tmp_path / "store"),
        project=project,
        session_spool_root=tmp_path,
        profile=LIVE_PROFILE,
        fl_license=FLLicense.TRIAL,
    )
    kernel = KernelHost(session, Workspace.create(tmp_path / "kernel"), PosixLauncher(), os.environ)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"fl": sdk.connect(session), "session": session, "kernel": kernel}
        )
    )
    yield SimpleNamespace(ctx=ctx, control=control, payloads=[])
    kernel.close()


def call(box: Any, tool: Any, **args: Any) -> Any:
    """Call an MCP tool as the model does and keep the payload it would see."""
    result = tool(ctx=box.ctx, **args)
    text = result.content[0].text if isinstance(result, CallToolResult) else result.model_dump_json()
    box.payloads.append(text)
    return result


def run(box: Any, code: str) -> Any:
    result = call(box, server.run, code=code)
    assert result.outcome == "ok", result.traceback
    return result


def test_write_then_read_on_a_trial_fl_is_definitive_and_plain(box):
    # Run bdea5e90 steps 0-3: look, set the tempo, write the kick.
    call(box, server.get_scene)
    run(box, "print(fl.snapshot()); print(fl.channels)")
    wrote = run(
        box,
        'print(fl.set_tempo(100.0))\n'
        'kick = theory.drums("x...x...x...x..." * 4, key="C1")\n'
        'print(fl.track("808 Kick").write(kick, mode="replace"))',
    )
    # Definitive: what was written, where, and the total - nothing to doubt.
    assert wrote.stdout == "100.0\nwrote=16 track='808 Kick' pattern=1 mode='replace' total=16\n"

    # Step 4 ("That status is still unconfirmed"): an immediate read returns it.
    assert run(box, 'print(len(fl.track("808 Kick").notes())); print(fl.tempo)').stdout == "16\n100.0\n"
    # Step 6: get_notes, with the channel as the model sent it (a string).
    notes = call(box, server.get_notes, pattern=1, channel="0")
    assert notes.note_count == 16 and len(notes.notes) == 16

    # Run b5343990 step 5 ("The scene seems stale, showing tempo 130 but no notes").
    scene = call(box, server.get_scene)
    assert scene.tempo == 100.0
    by_name = {c["name"]: c for c in scene.channels}
    assert by_name["808 Kick"]["notes"] == 16
    assert by_name["FLEX Bass"]["notes"] == 0, "an empty channel reads as empty, not missing"

    # Run b5343990 step 9: a one-note test write, read straight back.
    bass = run(box, 'b = fl.track("FLEX Bass")\nprint(b.write([Note(key="A2", start=0, length=1)], mode="replace"))\nb.notes()')
    assert bass.stdout == "wrote=1 track='FLEX Bass' pattern=1 mode='replace' total=1\n"
    assert bass.result == repr([Note(key=45, start=0, length=1)])
    merged = run(box, 'fl.track("FLEX Bass").write([Note(key="E3", start=1, length=1)])')
    assert merged.result == "WriteResult(wrote=1, track='FLEX Bass', pattern=1, mode='merge', total=2)"
    detail = call(box, server.get_scene, detail="notes", channel=4)
    assert [n.midi for n in detail.notes] == [45, 52]

    # Run bdea5e90 step 9: fl.save() on a trial says plainly that nothing is lost.
    saved = call(box, server.run, code="fl.save()")
    assert saved.outcome == "error"
    assert "This FL Studio is a trial, which cannot save" in saved.traceback
    assert saved.traceback.startswith('Traceback (most recent call last):\n  File "<fl>", line 1')
    assert "kernel" not in saved.traceback and "sdk.py" not in saved.traceback, "engine frames are hidden"

    # A bad argument reads like Python: the model's line, then the reason - no engine frames or chains.
    bad = call(box, server.run, code="fl.track(1.5)")
    assert bad.traceback.count("File ") == 1 and "ValidationError" not in bad.traceback.split("\n")[0]
    assert "During handling" not in bad.traceback and "direct cause" not in bad.traceback

    assert ("ctl.save", []) not in box.control.calls, "a trial FL is never asked to save"
    for payload in box.payloads:
        assert not INTERNAL_WORDS.search(payload), payload


def test_a_refused_write_is_a_plain_error_and_reads_stay_true(box):
    """A refusal raises (never a maybe), and the track still reads what FL has."""
    box.ctx.request_context.lifespan_context["session"].transport.responses["add_notes"] = SpoolResponse(
        id=0, ok=False, op="add_notes", err="protocol mismatch: command is v2, bridge is v3"
    )
    refused = call(box, server.run, code='fl.track("808 Kick").write([Note(key=36, start=0, length=1)])')
    assert refused.outcome == "error"
    assert "FL Studio refused the note edit, so nothing was written." in refused.traceback
    assert call(box, server.get_notes, pattern=1, channel=0).note_count == 0
    for payload in box.payloads:
        assert not INTERNAL_WORDS.search(payload), payload


# -- every model-facing string -------------------------------------------------


def test_tool_descriptions_and_schemas_speak_plainly():
    for tool in build_contract()["tools"]:
        # fl_doctor's report is the operator's setup diagnostic, and harnesses
        # read its fields (project_file.detail locates the saved song), so only
        # its description is held to the model's vocabulary.
        schema = "" if tool["name"] == "fl_doctor" else json.dumps(tool["outputSchema"])
        text = tool["description"] + schema
        assert not INTERNAL_WORDS.search(text), (tool["name"], INTERNAL_WORDS.search(text))


def _sdk_docs() -> list[tuple[str, str]]:
    """Every docstring help() shows a model for the SDK and what it returns."""
    docs: list[tuple[str, str]] = []
    for cls in (sdk.FL, sdk.Track):
        docs.append((cls.__name__, cls.__doc__ or ""))
        for name, member in vars(cls).items():
            if not name.startswith("_"):
                docs.append((f"{cls.__name__}.{name}", inspect.getdoc(member) or ""))
    returned = [Note, Target, schemas.WriteResult, schemas.Channel, schemas.SessionState, schemas.ChannelLevels,
                schemas.PluginParam, schemas.AutomationPoint, schemas.AutomationResult]
    for model in returned:
        docs.append((model.__name__, model.__dict__.get("__doc__") or ""))
        docs.extend((f"{model.__name__}.{field}", field) for field in model.model_fields)
    return docs


def test_sdk_docstrings_and_returned_types_speak_plainly():
    for where, text in _sdk_docs():
        assert not INTERNAL_WORDS.search(where + " " + text), where


def test_returned_models_carry_no_internal_fields():
    for model in (schemas.WriteResult, schemas.AutomationResult):
        assert issubclass(model, BaseModel)
        assert not INTERNAL_WORDS.search(" ".join(model.model_fields)), model


_ERROR_TYPES = {"ToolError", "WriteRejected", "NotePlaneUnavailable", "ConcurrentWrite", "ToolErrorResult", "WireError"}


def _error_strings() -> list[tuple[str, str]]:
    """Every string literal inside a construction of a model-facing error, engine-wide."""
    found: list[tuple[str, str]] = []
    for path in sorted((Path(sdk.__file__).parent).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            is_error_super = name == "__init__" and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call)
            if name in _ERROR_TYPES or is_error_super:
                found.extend(
                    (f"{path.name}:{const.lineno}", const.value)
                    for const in ast.walk(node)
                    if isinstance(const, ast.Constant) and isinstance(const.value, str)
                )
    return found


def test_every_error_message_and_fix_speaks_plainly():
    strings = _error_strings()
    assert len(strings) > 50, "the scan must actually see the engine's errors"
    offenders = [(where, text) for where, text in strings if INTERNAL_WORDS.search(text)]
    assert offenders == []
