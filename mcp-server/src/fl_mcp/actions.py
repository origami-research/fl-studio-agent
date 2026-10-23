"""What the engine did during one tool call, as sentences a person can read.

Every tool result carries ``actions``: one past-tense sentence per change FL
confirmed or measurement the engine took during the call, in order, so a UI
can show "what the assistant did". Sentences are built from what each call
returned (FL's values, per docs/27), never from what was asked for. A call
that raised adds nothing, and so does a read.

``ActionLog.called`` is the one entry point for FL calls: the run kernel host
records every call a cell makes over the wire, and the check, render and
analyze tools record their own call the same way, so a check reads the same
from either path.

Only changes in a row merge, so the list keeps FL's order and every sentence
is true of the state it describes: three or more changes of one kind in a row
become one sentence listing every item ("Set volume on 5 tracks: kick 0.60,
..."), repeated undos become "Undid 4 actions in FL", and an identical
sentence repeated in a row is counted. Changes are told apart by what they
touch (channel and pattern), never by a name two channels may share.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fl_mcp.audio import AudioAnalysis
from fl_mcp.schemas import (
    AutomationResult,
    ChannelLevels,
    CheckResult,
    PluginParam,
    RenderReceipt,
    WriteResult,
)

log = logging.getLogger(__name__)

# Sentences are complete: the UI shows one line and expands it on click. This
# cap only stops a pathological one (thousands of tracks, a runaway name).
MAX_CHARS = 2000
# A call lists at most this many actions, then "and N more actions": a cell can
# loop on fl calls for as long as it likes (their time is not charged to it).
MAX_ACTIONS = 200
# This many changes of one kind in a row read as one sentence.
GROUP_AT = 3
# Calls that rewrite the saved song and reopen FL to apply it.
SONG_REWRITES = frozenset({"set_automation", "add_effect"})
# Calls held inside an unfinished fl.file_edit() block, past which they are only counted.
_MAX_HELD = 10_000


@dataclass(frozen=True)
class _Kind:
    """How one kind of change reads alone and grouped."""

    one: Callable[[str, Any], str]  # the sentence for one target and its value
    many: str  # the grouped sentence's lead: {n} counts targets, {tracks} the tracks they are on
    item: Callable[[str, Any], str]  # one target inside the grouped sentence
    adds: bool = False  # repeated changes to one target add up (merged notes) instead of the last standing
    group_at: int = GROUP_AT


@dataclass(frozen=True)
class _Entry:
    sentence: str
    kind: _Kind | None = None
    key: tuple[Any, ...] = ()  # entries with the same key in a row group together
    target: tuple[Any, ...] = ()  # what the change is to, e.g. (channel, pattern)
    label: str = ""  # how the target reads
    value: Any = None
    track: int | None = None  # the channel it is on, for counting and telling equal names apart


class _Run:
    """Entries in a row that read as one: one kind of change, or one repeated sentence."""

    def __init__(self, entry: _Entry) -> None:
        self.entry = entry
        self.count = 1
        self.first = [entry]  # enough to list a run too short to group
        self.values: dict[tuple[Any, ...], tuple[str, Any, int | None]] = {
            entry.target: (entry.label, entry.value, entry.track)
        }

    def takes(self, entry: _Entry) -> bool:
        if self.entry.kind is None:
            return entry.kind is None and entry.sentence == self.entry.sentence
        return entry.kind is not None and entry.key == self.entry.key

    def add(self, entry: _Entry) -> None:
        self.count += 1
        if len(self.first) < GROUP_AT:
            self.first.append(entry)
        kind = self.entry.kind
        if kind is None:
            return
        previous = self.values.get(entry.target)
        value = previous[1] + entry.value if kind.adds and previous is not None else entry.value
        self.values[entry.target] = (entry.label, value, entry.track)

    def sentences(self) -> list[str]:
        kind = self.entry.kind
        if kind is None:
            return [self.entry.sentence + (f" ({self.count} times)" if self.count > 1 else "")]
        if self.count < kind.group_at:
            return [entry.sentence for entry in self.first]
        if len(self.values) == 1:
            ((label, value, _),) = self.values.values()
            return [kind.one(label, value)]
        labels = [label for label, _, _ in self.values.values()]
        items = [
            kind.item(label if labels.count(label) == 1 else f"{label} (channel {track})", value)
            for label, value, track in self.values.values()
        ]
        tracks = _count(len({track for _, _, track in self.values.values()}), "track")
        return [kind.many.format(n=len(self.values), tracks=tracks) + ": " + ", ".join(items)]


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _level(value: float) -> str:
    return f"{value:.2f}"


def _bpm(value: float) -> str:
    return f"{round(value, 3):g}"


def _where(insert: int) -> str:
    return "the master" if insert == 0 else f"mixer track {insert}"


_MERGED = _Kind(
    one=lambda t, v: f"Wrote {_count(v, 'note')} to {t}",
    many="Wrote notes to {tracks}",
    item=lambda t, v: f"{t} ({v})",
    adds=True,
)
_REPLACED = _Kind(
    one=lambda t, v: f"Replaced the notes on {t} ({_count(v, 'note')})",
    many="Replaced the notes on {tracks}",
    item=lambda t, v: f"{t} ({v})",
)
_CLEARED = _Kind(
    one=lambda t, v: f"Cleared the notes on {t}",
    many="Cleared the notes on {tracks}",
    item=lambda t, v: t,
)
_VOLUME = _Kind(
    one=lambda t, v: f"Set {t} volume to {v}", many="Set volume on {tracks}", item=lambda t, v: f"{t} {v}"
)
_PAN = _Kind(one=lambda t, v: f"Set {t} pan to {v}", many="Set pan on {tracks}", item=lambda t, v: f"{t} {v}")
_PITCH = _Kind(
    one=lambda t, v: f"Set {t} pitch to {v} semitones", many="Set pitch on {tracks}", item=lambda t, v: f"{t} {v}"
)
_TEMPO = _Kind(one=lambda t, v: f"Set tempo to {v} BPM", many="Set tempo", item=lambda t, v: v)
_UNDO = _Kind(
    one=lambda t, v: "Undid the last action in FL" if v == 1 else f"Undid {v} actions in FL",
    many="Undid",
    item=lambda t, v: t,
    adds=True,
    group_at=2,
)


def _param_kind(track: str) -> _Kind:
    return _Kind(
        one=lambda t, v: f"Set {track} {t} to {v}",
        many=f"Set {{n}} parameters on {track}",
        item=lambda t, v: f"{t} {v}",
    )


class ActionLog:
    """The actions of one tool call, in the order FL confirmed them."""

    def __init__(self, track_name: Callable[[int], str] | None = None) -> None:
        self._track_name = track_name or (lambda channel: f"Channel {channel}")
        self._names: dict[int, str] = {}
        self._runs: list[_Run] = []
        self._more = 0  # actions past MAX_ACTIONS, only counted
        self._held: list[tuple[str, Mapping[str, Any], Any]] = []
        self._held_more = 0
        self._holding = 0
        self._hold_failed = False

    def called(self, method: str, kwargs: Mapping[str, Any], value: Any) -> None:
        """Record an FL session call that returned. Never raises: FL already has
        the change, so a sentence that cannot be written is logged, not turned
        into an error for a call that succeeded."""
        if method not in _CALLS:
            return
        if self._holding:
            if len(self._held) < _MAX_HELD:
                self._held.append((method, kwargs, value))
            else:
                self._held_more += 1
            return
        self._describe(method, kwargs, value)
        if method in SONG_REWRITES:
            self._add(_Entry("Reopened FL with the changed song"))

    def hold(self) -> None:
        """An fl.file_edit() block opened: its changes reach FL only when it closes."""
        self._holding += 1

    def release(self, applied: bool) -> None:
        """A block closed; once the outermost closes and FL reopened with every
        change, they are recorded, in order. A failed close records none of them."""
        self._holding -= 1
        self._hold_failed = self._hold_failed or not applied
        if self._holding:
            return
        held, self._held = self._held, []
        more, self._held_more = self._held_more, 0
        failed, self._hold_failed = self._hold_failed, False
        if failed:
            log.warning("a file_edit block failed to close; its %d changes are not listed", len(held) + more)
            return
        for call in held:
            self._describe(*call)
        self._more += more
        if any(method in SONG_REWRITES for method, _, _ in held):
            self._add(_Entry("Reopened FL with the changed song"))

    def measured(self, analysis: AudioAnalysis, wav: str) -> None:
        """Record a measurement of a WAV; ``wav`` names it ("the render", "mix.wav")."""
        self._add(_Entry(_measured(analysis, wav)))

    def sentences(self) -> list[str]:
        out = [_fit(sentence) for run in self._runs for sentence in run.sentences()]
        more = self._more + max(0, len(out) - MAX_ACTIONS)
        out = out[:MAX_ACTIONS]
        if more:
            out.append(f"and {_count(more, 'more action')}")
        return out

    def _describe(self, method: str, kwargs: Mapping[str, Any], value: Any) -> None:
        try:
            entries = _CALLS[method](self, kwargs, value)
        except Exception:
            log.exception("could not describe the %s call for the tool result's actions", method)
            return
        for entry in entries:
            self._add(entry)

    def _add(self, entry: _Entry) -> None:
        if self._more:  # past the cap, so the last listed run is no longer the latest
            self._more += 1
        elif self._runs and self._runs[-1].takes(entry):
            self._runs[-1].add(entry)
        elif len(self._runs) < MAX_ACTIONS:
            self._runs.append(_Run(entry))
        else:
            self._more += 1

    # -- one describer per FL session call that changes or measures something ----

    def _name(self, channel: int) -> str:
        if channel not in self._names:
            self._names[channel] = self._track_name(channel)
        return self._names[channel]

    @staticmethod
    def _notes_target(kwargs: Mapping[str, Any], result: WriteResult) -> tuple[tuple[int, int], str, int]:
        target = kwargs["target"]
        label = result.track if result.pattern == 1 else f"{result.track} in pattern {result.pattern}"
        return (target.channel, target.pattern), label, target.channel

    def _write(self, kwargs: Mapping[str, Any], result: WriteResult) -> list[_Entry]:
        target, label, track = self._notes_target(kwargs, result)
        if result.mode == "merge":
            kind, value = _MERGED, result.wrote
        else:  # FL's count afterwards, which is what the track now has
            kind, value = _REPLACED, result.wrote if result.total is None else result.total
        return [_Entry(kind.one(label, value), kind, ("notes", result.mode), target, label, value, track)]

    def _clear(self, kwargs: Mapping[str, Any], result: WriteResult) -> list[_Entry]:
        target, label, track = self._notes_target(kwargs, result)
        return [_Entry(_CLEARED.one(label, None), _CLEARED, ("clear",), target, label, None, track)]

    def _levels(self, kwargs: Mapping[str, Any], result: ChannelLevels) -> list[_Entry]:
        channel = result.channel
        track = self._name(channel)
        pitch = f"{result.pitch_semitones:+d}" if result.pitch_semitones else "0"
        fields = (
            ("volume", _VOLUME, _level(result.volume)),
            ("pan", _PAN, _level(result.pan)),
            ("pitch_semitones", _PITCH, pitch),
        )
        return [
            _Entry(kind.one(track, value), kind, (name,), (channel,), track, value, channel)
            for name, kind, value in fields
            if kwargs.get(name) is not None
        ]

    def _param(self, kwargs: Mapping[str, Any], result: PluginParam) -> list[_Entry]:
        channel = kwargs["channel"]
        track = self._name(channel)
        value = _level(result.value) if result.kind == "continuous" or not result.display else result.display
        kind = _param_kind(track)
        return [_Entry(kind.one(result.name, value), kind, ("param", channel), (result.index,), result.name, value, channel)]

    def _tempo(self, kwargs: Mapping[str, Any], bpm: float) -> list[_Entry]:
        return [_Entry(_TEMPO.one("tempo", _bpm(bpm)), _TEMPO, ("tempo",), ("tempo",), "tempo", _bpm(bpm))]

    def _automation(self, kwargs: Mapping[str, Any], result: AutomationResult) -> list[_Entry]:
        track = self._name(result.channel)
        return [_Entry(f"Replaced the automation on {track} ({_count(len(result.points), 'point')})")]

    def _effect(self, kwargs: Mapping[str, Any], effects: tuple[Any, ...]) -> list[_Entry]:
        # The names are FL's readback of that insert after the change.
        names = [effect.name for effect in effects]
        where = _where(kwargs["insert_index"])
        if kwargs["plugin_name"] in names:
            return [_Entry(f"Added {kwargs['plugin_name']} to {where}")]
        return [_Entry(f"Changed the effects on {where}; it now has {', '.join(names) or 'none'}")]

    def _check(self, kwargs: Mapping[str, Any], result: CheckResult) -> list[_Entry]:
        return [_Entry(_checked(result))]

    def _render(self, kwargs: Mapping[str, Any], receipt: RenderReceipt) -> list[_Entry]:
        played = _duration(receipt.span.seconds)
        if receipt.span.source == "pattern" and receipt.span.pattern is not None:
            return [_Entry(f"Rendered {played} of pattern {receipt.span.pattern}")]
        return [_Entry(f"Rendered {played}")]

    def _export(self, kwargs: Mapping[str, Any], path: Path) -> list[_Entry]:
        return [_Entry(f"Exported the song to {Path(path).name}")]

    def _save(self, kwargs: Mapping[str, Any], value: Any) -> list[_Entry]:
        return [_Entry("Saved the song")]

    def _undo(self, kwargs: Mapping[str, Any], value: Any) -> list[_Entry]:
        return [_Entry(_UNDO.one("", 1), _UNDO, ("undo",), ("undo",), "", 1)]


_CALLS: dict[str, Callable[[ActionLog, Mapping[str, Any], Any], list[_Entry]]] = {
    "write_notes": ActionLog._write,
    "clear_notes": ActionLog._clear,
    "set_channel_levels": ActionLog._levels,
    "set_plugin_param": ActionLog._param,
    "set_tempo": ActionLog._tempo,
    "set_automation": ActionLog._automation,
    "add_effect": ActionLog._effect,
    "check": ActionLog._check,
    "render_wav": ActionLog._render,
    "export_offline": ActionLog._export,
    "save_project": ActionLog._save,
    "undo": ActionLog._undo,
}


def _checked(result: CheckResult) -> str:
    last = result.start_bar + result.bars - 1
    span = f"bar {result.start_bar}" if result.bars == 1 else f"bars {result.start_bar}-{last}"
    heard = sum(1 for c in result.channels if c.sounded)
    shared = sum(1 for c in result.channels if c.sounded is None)
    parts = [f"{heard} of {len(result.channels)} tracks sound"]
    if shared:
        parts.append(f"{shared} not measurable alone")
    master = result.master_peak_dbfs
    parts.append(f"master {master:.1f} dBFS" if master is not None else "master silent")
    return f"Checked {span}: " + ", ".join(parts)


def _measured(analysis: AudioAnalysis, wav: str) -> str:
    if not math.isfinite(analysis.true_peak_dbfs):
        return f"Measured {wav}: it is silent"
    parts = [f"peak {analysis.true_peak_dbfs:.1f} dBFS"]
    if math.isfinite(analysis.integrated_lufs):
        parts.append(f"{analysis.integrated_lufs:.1f} LUFS")
    balance = analysis.spectral_balance
    if math.isfinite(balance.low_dbfs) and math.isfinite(balance.mid_dbfs):
        lows = round(balance.low_dbfs - balance.mid_dbfs)
        parts.append(
            f"lows {lows} dB over mids" if lows > 0 else f"lows {-lows} dB under mids" if lows < 0 else "lows level with mids"
        )
    if analysis.bpm is not None:
        parts.append(f"{analysis.bpm:.0f} BPM")
    if analysis.key is not None:
        parts.append(f"key {analysis.key}")
    return f"Measured {wav}: " + ", ".join(parts)


def _duration(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _fit(sentence: str) -> str:
    return sentence if len(sentence) <= MAX_CHARS else sentence[: MAX_CHARS - 3].rstrip() + "..."


# -- the workspace tools (docs/39) ----------------------------------------------------
# Each changing call is one sentence; reads (read_file, glob, grep) add none.

# How much of a command's first line its sentence shows.
COMMAND_CHARS = 80


def ran_command(command: str, exit_code: int | None, seconds: float, timeout_s: int) -> str:
    """'Ran "pip install mido" (exit 0, 3.1 s)': the command's first line, clipped."""
    first = command.strip().split("\n", 1)[0].strip()
    if len(first) > COMMAND_CHARS or first != command.strip():
        first = first[: COMMAND_CHARS - 3].rstrip() + "..."
    outcome = f"timed out after {timeout_s} s" if exit_code is None else f"exit {exit_code}, {seconds:.1f} s"
    return f'Ran "{first}" ({outcome})'


def wrote_file(path: str, size: int) -> str:
    return _fit(f"Wrote {path} ({file_size(size)})")


def edited_file(path: str, replacements: int) -> str:
    return _fit(f"Edited {path} ({_count(replacements, 'change')})")


def file_size(size: int) -> str:
    if size < 1024:
        return _count(size, "byte")
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
