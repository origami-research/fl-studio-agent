"""Decompile a saved .flp into a construction program.

Given a finished project, emit the ordered series of tool calls that rebuilds
it from empty (docs/18). This is decompilation, not history recovery: FL throws
its real undo stack away on save, so we produce *a* valid program that yields
the same project, the way a decompiler produces equivalent source - not the
original keystrokes.

Coverage is the intersection of "what the parser reliably reads" and "what our
tools can write": tempo, channels, and notes today; arrangement and automation
join as the FILE-write frontier fills in (docs/18). Verified by round-trip:
replay the program on an empty project, re-parse, diff against the original.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


@dataclass
class Step:
    """One construction action: a tool name, its args, and why it's here."""

    op: str
    args: dict[str, Any]
    comment: str = ""


@dataclass
class Program:
    """An ordered construction program that rebuilds `source` from empty."""

    source: str
    steps: list[Step] = field(default_factory=list)


def decompile(flp_path: str | Path) -> Program:
    """Parse a .flp and emit the ordered program that reconstructs it.

    Order matters and encodes dependencies: tempo first, then channels must
    exist before notes can be written to them, then notes per pattern/channel.
    """
    from harness.flp_compat import import_pyflp, parse
    from harness.verify import read_notes

    import_pyflp()
    project = parse(str(flp_path))
    program = Program(source=str(flp_path))

    # 1. Global settings.
    program.steps.append(Step("set_tempo", {"bpm": float(project.tempo)}, "project tempo"))

    # 2. Channels - they must exist before any notes reference them. Name falls
    #    back to the rack index when the project left it unnamed.
    channel_names: dict[int, str] = {}
    for ch in project.channels:
        iid = int(getattr(ch, "iid", len(channel_names)))
        name = ch.name or f"Channel {iid}"
        channel_names[iid] = name
        program.steps.append(
            Step("add_channel", {"name": name, "kind": type(ch).__name__}, f"channel {iid}")
        )

    # 3. Notes, grouped by (pattern, channel). read_notes is the same reader the
    #    reconstruction round-trip and read-back use, proven on the corpus.
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for r in read_notes(Path(flp_path)):
        grouped.setdefault((r.pattern, r.channel), []).append(
            {
                "key": r.midi,
                "start": round(r.start_beat, 6),
                "length": round(r.length_beats, 6),
                "velocity": round(r.velocity / 127.0, 4),  # FL 0-127 -> our 0-1
            }
        )
    for (pattern, channel), notes in sorted(grouped.items()):
        program.steps.append(
            Step(
                "write_notes",
                {"pattern": pattern, "channel": channel, "mode": "replace", "notes": notes},
                f"{len(notes)} note(s) on channel {channel}, pattern {pattern}",
            )
        )

    return program


def to_python(program: Program) -> str:
    """Render a program as runnable kernel-style Python (the readable form)."""
    lines = [f"# reconstruct {Path(program.source).name}", "def build(fl):"]
    for step in program.steps:
        arg_str = ", ".join(f"{k}={v!r}" for k, v in step.args.items())
        comment = f"  # {step.comment}" if step.comment else ""
        lines.append(f"    fl.{step.op}({arg_str}){comment}")
    return "\n".join(lines)
