"""Lane K test seam: a fake FLSession wired to fakes, per docs/21.

``sdk.connect(fake)`` is the deliberate injection point. FL IO is faked; the
Store is REAL (on tmp_path) so the write-through path - record_write, deltas,
the action log - is the shipped code, not a mock of it.
"""

from __future__ import annotations

from pathlib import Path

from fl_mcp.schemas import (
    AutomationPoint,
    AutomationReceipt,
    Channel,
    ChannelLevels,
    ControlResponse,
    Note,
    NoteWriteMode,
    PluginParam,
    RenderReceipt,
    SessionState,
    Target,
    WriteReceipt,
)
from fl_mcp.session import FLSession
from fl_mcp.store import Store


class FakeControl:
    """Minimal control plane: records ops, flips transport state, fails on demand."""

    def __init__(self) -> None:
        self.playing = False
        self.ops: list[str] = []
        self.fail_with: str | None = None

    def send_request(self, op, args=None, kwargs=None, timeout=2.0) -> ControlResponse:
        self.ops.append(op)
        if self.fail_with is not None:
            return ControlResponse(id=len(self.ops), ok=False, err=self.fail_with)
        if op == "transport.start":
            self.playing = True
        elif op == "transport.stop":
            self.playing = False
        return ControlResponse(id=len(self.ops), ok=True)


class FakeSession(FLSession):
    """Duck-typed FLSession whose backend is the in-memory store itself.

    Every write really lands in the injected (real) Store and every read comes
    back from it, so receipts carry genuine readback counts and deltas.
    ``fail_next_write`` simulates a backend rejection: the exception raises
    BEFORE anything is applied, exactly like session.write_notes promises.
    """

    def __init__(self, *, store: Store, channels=(), control=None, tempo: float | None = 120.0):
        self.control = control
        self.store = store
        self.project = None
        self.tempo = tempo
        self.fail_next_write: Exception | None = None
        self._channels = list(channels)
        self._next_id = 0
        self.level_calls: list[tuple[int, float | None, float | None, int | None]] = []

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        if self.fail_next_write is not None:
            exc, self.fail_next_write = self.fail_next_write, None
            raise exc
        self._next_id += 1
        delta = self.store.record_write(target, notes, mode, "project-write", self._next_id)
        confirmed = self.store.notes_at(target) or []
        return WriteReceipt(
            backend="project-write", target=target, confirmed_notes=len(confirmed), delta=delta
        )

    def clear_notes(self, target: Target) -> WriteReceipt:
        return self.write_notes(target, [], "replace")

    def read_notes(self, target: Target) -> list[Note]:
        return self.store.notes_at(target) or []

    def set_tempo(self, bpm: float) -> float:
        # FL stores tempo as u32 BPM x 1000; the readback is that quantized
        # value, not the caller's echo.
        self.tempo = round(bpm * 1000) / 1000
        return self.tempo

    def render_wav(self, output: Path) -> RenderReceipt:
        output.write_bytes(b"RIFF....WAVE")
        return RenderReceipt(path=output, project_saved=True)

    def export_offline(self, output: Path) -> Path:
        return self.render_wav(output).path

    def save_project(self) -> int | None:
        if self.control is None:
            from fl_mcp import errors

            raise errors.control_plane_unavailable("ctl.save", 0.0)
        return self.control.send_request("ctl.save").ret

    def set_automation(self, channel: int, points: list[AutomationPoint]) -> AutomationReceipt:
        return AutomationReceipt(backend="project-write", channel=channel, points=points)

    def set_channel_levels(
        self,
        channel: int,
        *,
        volume: float | None = None,
        pan: float | None = None,
        pitch_semitones: int | None = None,
    ) -> ChannelLevels:
        self.level_calls.append((channel, volume, pan, pitch_semitones))
        return ChannelLevels(
            channel=channel,
            volume=0.8 if volume is None else volume,
            pan=0.0 if pan is None else pan,
            pitch_semitones=0 if pitch_semitones is None else pitch_semitones,
        )

    def plugin_params(
        self, channel: int, *, query: str | None = None, limit: int = 40
    ) -> list[PluginParam]:
        params = [
            PluginParam(index=0, name="Cutoff", value=0.25, display="0.25", kind="continuous"),
            PluginParam(index=1, name="Resonance", value=0.5, display="0.5", kind="continuous"),
        ]
        if query is not None:
            params = [param for param in params if query.lower() in param.name.lower()]
        return params[:limit]

    def set_plugin_param(self, channel: int, param: int | str, value: float) -> PluginParam:
        name = "Cutoff" if param == 0 else str(param)
        return PluginParam(
            index=0 if isinstance(param, str) else param,
            name=name,
            value=value,
            display=f"{value:g}",
            kind="continuous",
        )

    def snapshot(self) -> SessionState:
        return SessionState(tempo_bpm=self.tempo, channels=list(self._channels))


DEFAULT_CHANNELS = (
    Channel(index=0, name="Kick"),
    Channel(index=1, name="Bass"),
    Channel(index=2, name="Lead"),
)
