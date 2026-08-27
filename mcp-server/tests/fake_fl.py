"""Fake FL Studio: stand-ins for both in-FL endpoints (docs/02, docs/03).

Everything the server can talk to inside FL is one of two programs; this file
plays both so server/kernel logic is testable in CI with no FL Studio and no
real MIDI ports installed:

- ``FakeBridge`` — the v1 note plane. Mimics the native Bridge plugin's
  Idle-poll spool consumer (docs/04 "Core loop"): claims commands from a
  SpoolSession's ``commands/`` dir in lexical order, executes them against an
  in-memory piano-roll model, writes atomic responses, and is idempotent by
  command id. (The plugin isn't wired into a live server yet — docs/12 — so
  this exercises the kernel's targeted write path exactly as honestly as the
  real thing will once it lands.)
- ``FakeControl`` — the control plane. Stands in for
  ``control_midi.MidiControl`` — same ``send_request``/``add_event_listener``/
  ``await_hello``/``ping`` surface, driven directly against an in-memory FL
  state model. The SysEx wire codec itself (framing, chunking, correlation) is
  fl-scripts' own frozen surface and is covered by
  fl-scripts/controller/tests/test_framing.py and control_midi's own tests —
  this fake exists to test kernel/session logic, not the codec.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fl_mcp import errors, schemas, spool

# ---------------------------------------------------------------------------
# Note plane (v1): FakeBridge
# ---------------------------------------------------------------------------

PLUGIN_VER = "0.0.1-fake"


@dataclass
class _TargetState:
    notes: list[dict] = field(default_factory=list)


class FakeBridge:
    """Stand-in for the native Bridge plugin's spool consumer."""

    def __init__(self, session: spool.SpoolSession, max_per_tick: int = 4) -> None:
        self.session = session
        self.max_per_tick = max_per_tick
        self.targets: dict[tuple[int, int], _TargetState] = {}
        self._processed_ids: set[int] = set()
        self._thread: Any | None = None
        self._stop = None
        self.fail_ops: set[str] = set()  # op names to force ok:false, for error-path tests

    # -- state inspection ---------------------------------------------------

    def notes_at(self, pattern: int, channel: int) -> list[dict]:
        state = self.targets.get((pattern, channel))
        return list(state.notes) if state else []

    # -- the Idle-poll loop ---------------------------------------------------

    def pump(self) -> int:
        """Process up to max_per_tick pending commands. Returns how many ran."""
        pending = sorted(
            (p for p in self.session.commands_dir.glob("*.json") if p.stem.isdigit()),
            key=lambda p: int(p.stem),
        )
        ran = 0
        for path in pending[: self.max_per_tick]:
            self._claim(path)
            ran += 1
        return ran

    def _claim(self, path: Path) -> None:
        payload = spool.read_json(path)
        if payload is None:
            return
        cmd_id = payload.get("id")
        if cmd_id in self._processed_ids:
            return
        response = self._execute(payload)
        response["plugin_ver"] = PLUGIN_VER
        spool.write_json_atomic(self.session.responses_dir / f"{cmd_id:06d}.json", response)
        self._processed_ids.add(cmd_id)
        done_path = self.session.done_dir / path.name
        path.replace(done_path)

    def _execute(self, cmd: dict) -> dict:
        cmd_id = cmd.get("id")
        op = cmd.get("op")
        if op in self.fail_ops:
            return {"id": cmd_id, "ok": False, "op": op, "err": f"forced failure for {op}"}
        handler = self._HANDLERS.get(op)
        if handler is None:
            return {"id": cmd_id, "ok": False, "op": op, "err": f"unknown op {op!r}"}
        try:
            return handler(self, cmd_id, cmd.get("args", {}))
        except Exception as exc:  # the real plugin never lets exceptions escape either
            return {"id": cmd_id, "ok": False, "op": op, "err": f"{type(exc).__name__}: {exc}"}

    def _target_state(self, target: schemas.Target) -> _TargetState:
        key = (int(target.pattern), int(target.channel))
        return self.targets.setdefault(key, _TargetState())

    def _add_notes(self, cmd_id: int, raw_args: dict) -> dict:
        args = schemas.AddNotesArgs.model_validate(raw_args)
        state = self._target_state(args.target)
        if args.mode == "replace":
            state.notes = []
        state.notes.extend(n.canonical() for n in args.notes)
        return {"id": cmd_id, "ok": True, "op": "add_notes", "notes_written": len(args.notes)}

    def _clear_notes(self, cmd_id: int, raw_args: dict) -> dict:
        args = schemas.ClearNotesArgs.model_validate(raw_args)
        state = self._target_state(args.target)
        cleared = len(state.notes)
        state.notes = []
        return {"id": cmd_id, "ok": True, "op": "clear_notes", "notes_written": cleared}

    def _preview_note(self, cmd_id: int, raw_args: dict) -> dict:
        schemas.PreviewNoteArgs.model_validate(raw_args)  # writes nothing; validate + ack
        return {"id": cmd_id, "ok": True, "op": "preview_note"}

    def _ping(self, cmd_id: int, raw_args: dict) -> dict:
        schemas.PingArgs.model_validate(raw_args)
        return {"id": cmd_id, "ok": True, "op": "ping"}

    _HANDLERS = {
        "add_notes": _add_notes,
        "clear_notes": _clear_notes,
        "preview_note": _preview_note,
        "ping": _ping,
    }

    # -- background thread, for tests that exercise SpoolSession's async wait --

    def start(self, interval: float = 0.005) -> None:
        import threading

        if self._thread is not None:
            return
        self._stop = threading.Event()

        def loop() -> None:
            while not self._stop.is_set():
                self.pump()
                time.sleep(interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None


# ---------------------------------------------------------------------------
# Control plane: FakeControl
# ---------------------------------------------------------------------------


@dataclass
class FakeChannelState:
    number: int
    name: str
    volume: float = 0.8
    pan: float = 0.5
    muted: bool = False


@dataclass
class FakeMixerTrack:
    number: int
    name: str
    volume: float = 0.8
    pan: float = 0.5
    muted: bool = False
    solo: bool = False


class FakeControl:
    """Stand-in for ``control_midi.MidiControl``.

    Implements the same duck-typed surface kernel.py/session.py depend on
    (``send_request``, ``add_event_listener``, ``await_hello``, ``ping``),
    driven directly against an in-memory FL state model — no SysEx encoding,
    since the wire codec is exercised elsewhere (see module docstring).
    """

    def __init__(
        self,
        ppq: int = 96,
        fl_ver: str = "2025.0",
        script_ver: str = "0.0.1-fake",
        channels: list[FakeChannelState] | None = None,
        mixer: list[FakeMixerTrack] | None = None,
    ) -> None:
        self.ppq = ppq
        self.fl_ver = fl_ver
        self.script_ver = script_ver
        self.tempo = 140.0
        self.playing = False
        self.song_pos = 0
        self.current_pattern = 0
        self.pattern_count = 1
        self.channels = channels if channels is not None else [FakeChannelState(0, "Kick")]
        self.mixer = mixer if mixer is not None else [FakeMixerTrack(0, "Master")]
        self.fail_ops: set[str] = set()
        self.hello: schemas.HelloEvent | None = None
        self._event_listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_event_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._event_listeners.append(fn)

    def send_hello(self) -> None:
        self.hello = schemas.HelloEvent(
            script_ver=self.script_ver, fl_ver=self.fl_ver, ppq=self.ppq, tempo=self.tempo
        )
        self._fire("hello", script_ver=self.script_ver, fl_ver=self.fl_ver, ppq=self.ppq)

    def send_event(self, op: str, **fields: Any) -> None:
        """Fire a spontaneous event (docs/11 Layer 1: dirty_channel,
        dirty_mixer_track, refresh, project_load), exactly as device_FLMCP.py's
        _send_event does on the real OnDirtyChannel/OnRefresh/OnProjectLoad
        callbacks."""
        self._fire(op, **fields)

    def _fire(self, op: str, **fields: Any) -> None:
        payload = {"op": op, **fields}
        for fn in self._event_listeners:
            fn(payload)

    def await_hello(self, timeout: float = 5.0) -> schemas.HelloEvent:
        if self.hello is None:
            raise errors.control_plane_unavailable("hello", timeout)
        return self.hello

    def ping(self, timeout: float = 2.0) -> bool:
        return self.send_request("ping", timeout=timeout).ok

    def send_request(
        self,
        op: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> schemas.ControlResponse:
        args = args or []
        if op in self.fail_ops:
            return schemas.ControlResponse(id=0, ok=False, err=f"forced failure for {op}")
        if op == "ping":
            return schemas.ControlResponse(id=0, ok=True, ret="pong")
        if op == "state.snapshot":
            return schemas.ControlResponse(id=0, ok=True, ret=self._snapshot())
        handler = self._OPS.get(op)
        if handler is None:
            return schemas.ControlResponse(id=0, ok=False, err=f"unknown op {op}")
        try:
            ret = handler(self, *args)
        except Exception as exc:
            return schemas.ControlResponse(id=0, ok=False, err=f"{type(exc).__name__}: {exc}")
        return schemas.ControlResponse(id=0, ok=True, ret=ret)

    def _snapshot(self) -> dict:
        return {
            "ppq": self.ppq,
            "tempo": self.tempo,
            "playing": self.playing,
            "song_pos": self.song_pos,
            "current_pattern": self.current_pattern,
            "pattern_count": self.pattern_count,
            "channels": [
                {"idx": c.number, "name": c.name, "muted": c.muted, "vol": c.volume, "pan": c.pan}
                for c in self.channels
            ],
            "mixer": [
                {"idx": t.number, "name": t.name, "vol": t.volume, "pan": t.pan, "muted": t.muted, "solo": t.solo}
                for t in self.mixer
            ],
        }

    def _transport_start(self) -> None:
        self.playing = True

    def _transport_stop(self) -> None:
        self.playing = False
        self.song_pos = 0

    def _transport_is_playing(self) -> bool:
        return self.playing

    def _set_tempo(self, bpm: float) -> None:
        self.tempo = float(bpm)

    def _channels_count(self) -> int:
        return len(self.channels)

    def _mixer_count(self) -> int:
        return len(self.mixer)

    def _general_undo(self) -> None:
        pass  # no undo-stack model on the fake; the kernel only needs the round trip to ack

    _OPS = {
        "transport.start": _transport_start,
        "transport.stop": _transport_stop,
        "transport.isPlaying": _transport_is_playing,
        "mixer.setCurrentTempo": _set_tempo,
        "general.undo": _general_undo,
        "channels.channelCount": _channels_count,
        "mixer.trackCount": _mixer_count,
    }
