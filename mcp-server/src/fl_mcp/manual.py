"""The model's manual, generated: the run tool's description and docs/sdk-reference.md.

The SDK's docstrings (sdk.py, theory.py, schemas.Note) and the guides' front matter
(guides/) are the only source. This module only arranges them: the run description is
run's own contract plus a one-line index per SDK area and the guide index, short enough
that no client cuts it (Claude Code cuts a tool description loaded upfront at 4,096
characters); the reference page is every member's signature and full docstring, the
same text ``help()`` shows in run.

Regenerate the page with ``uv run python -m fl_mcp.manual > ../docs/sdk-reference.md``
from mcp-server/; a test fails when the checked-in page or contract is stale.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from pydantic import BaseModel

from fl_mcp import contract, guides, schemas, sdk, theory
from fl_mcp.schemas import Note

# Claude Code's limit on a tool description loaded upfront and on server instructions.
DESCRIPTION_MAX_CHARS = 4_096

# Which area each SDK member is listed under, in the order the model meets them. A test
# fails when a public member of FL, Track, Effect or theory is missing here, so a new
# method cannot be left out of the index.
AREAS: dict[str, tuple[str, ...]] = {
    "Read": ("fl.scene", "fl.tempo", "fl.channels", "fl.mixer", "fl.playlist", "fl.patterns"),
    "Notes": (
        "fl.track", "Track.write", "Track.notes", "Track.clear", "Note",
        "theory.chord", "theory.scale", "theory.drums", "theory.transpose", "theory.quantize",
    ),
    "Sound": (
        "fl.sounds", "fl.audition", "fl.load_sample", "fl.load_preset", "fl.presets", "fl.set_preset",
        "Track.params", "Track.set_param",
    ),
    "Mix": ("Track.set_levels", "fl.route", "fl.send", "fl.effect", "Effect.params", "Effect.set_param"),
    "Song": ("fl.set_tempo", "fl.new_pattern", "fl.rename_pattern", "fl.place", "fl.automate"),
    "Hear": ("fl.meters", "fl.render", "fl.read_wav"),
    "Project": ("fl.save", "fl.undo", "fl.export_offline", "fl.file_edit"),
    "Guides": ("fl.guide",),
}

# What each prefix in AREAS names, and how the model writes it in a cell.
_OWNERS: dict[str, tuple[Any, str]] = {
    "fl": (sdk.FL, "fl"),
    "Track": (sdk.Track, "track"),
    "Effect": (sdk.Effect, "effect"),
    "theory": (theory, "theory"),
}


def member(entry: str) -> Any:
    """The object an AREAS entry names."""
    if entry == "Note":
        return Note
    owner, _, name = entry.partition(".")
    return inspect.getattr_static(_OWNERS[owner][0], name)


def signature(entry: str, full: bool = True) -> str:
    """How the model calls it, ``fl.render(output, *, bars=None, ...)``, and a property
    bare. Not ``full``: the required arguments only, and "..." when there are more."""
    owner, _, name = entry.partition(".")
    if entry == "Note":
        shown = "Note"
        params = [
            inspect.Parameter(field_name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              default=inspect.Parameter.empty if field.is_required() else field.default,
                              annotation=field.annotation.__name__)
            for field_name, field in Note.model_fields.items()
        ]
    else:
        obj = member(entry)
        shown = f"{_OWNERS[owner][1]}.{name}"
        if isinstance(obj, property):
            returned = inspect.signature(obj.fget).return_annotation
            return f"{shown}: {returned}" if full else shown
        params = [p for p in inspect.signature(obj).parameters.values() if p.name != "self"]
    if full:
        return f"{shown}({', '.join(_parameters(params))}){_returns(entry)}"
    required = [p.name for p in params if p.default is p.empty]
    more = ["..."] if len(required) < len(params) else []
    return f"{shown}({', '.join(required + more)})"


def _parameters(params: list[inspect.Parameter]) -> list[str]:
    """Each argument as the source writes it: ``bars: int | None = None``, with ``*`` before
    the keyword-only ones."""
    parts: list[str] = []
    for param in params:
        if param.kind is param.KEYWORD_ONLY and "*" not in parts:
            parts.append("*")
        if param.annotation is param.empty:
            parts.append(param.name if param.default is param.empty else f"{param.name}={param.default!r}")
            continue
        text = f"{param.name}: {param.annotation}"
        parts.append(text if param.default is param.empty else f"{text} = {param.default!r}")
    return parts


def _returns(entry: str) -> str:
    if entry == "Note":
        return ""
    returned = inspect.signature(member(entry)).return_annotation
    return "" if returned is inspect.Signature.empty else f" -> {returned}"


def sdk_index() -> str:
    """One line per area: the call of every member in it."""
    width = max(len(area) for area in AREAS)
    return "\n".join(
        f"  {area.ljust(width)}  " + " ".join(_short(entry) for entry in entries) for area, entries in AREAS.items()
    )


# Facts about an FL session that the model cannot guess, and nothing about how to work:
# the brief and the model own the musical choices and the workflow (owner, 2026-11-15).
SESSION_FACTS = """Good to know:
- print(fl.scene()) shows the whole project in one digest: patterns, channels, playlist and mixer.
- You hear by measuring: fl.meters() takes seconds, a render plays in real time."""

NO_SESSION = "This kernel has no FL Studio session: every fl call is refused except fl.guide()."


def tool_description(kernel: str, *, with_fl: bool) -> str:
    """The run tool's description: the kernel's contract (run's docstring), then with an FL
    session the session facts and the SDK index, and in both cases the guide index, all
    generated from their sources."""
    found = guides.load()
    guide_lines = "\n".join(f"  {line}" for line in guides.index(found).splitlines())
    if with_fl:
        fl_doc = " ".join(inspect.getdoc(sdk.FL).split())
        session = (
            f"{SESSION_FACTS}\n\n"
            f"fl: {fl_doc} help(fl.render) gives a member's arguments, defaults, results and "
            f"limits; read it before first use.\n{sdk_index()}"
        )
    else:
        session = NO_SESSION
    return (
        f"{inspect.cleandoc(kernel)}\n\n{session}\n\n"
        f"Guides: optional reference, not instructions, each with a worked example. "
        f"fl.guide(\"{found[0].name}\") prints one:\n"
        f"{guide_lines}"
    )


def reference_markdown() -> str:
    """docs/sdk-reference.md: every member's signature and docstring, by area, and the guides."""
    out = [
        "# SDK reference",
        "",
        "<!-- Generated by fl_mcp/manual.py from the SDK's docstrings; do not edit. Regenerate:",
        "     uv run python -m fl_mcp.manual > ../docs/sdk-reference.md (from mcp-server/) -->",
        "",
        "The `fl` object in the `run` tool is the live FL Studio session.",
        "In `run`, `help(fl.render)` shows the same text as each entry below.",
        "",
        inspect.getdoc(sdk.FL) or "",
    ]
    for area, entries in AREAS.items():
        out += ["", f"## {area}"]
        for entry in entries:
            out += ["", f"### `{signature(entry)}`", "", _doc(entry)]
    out += ["", "## What the calls return"]
    for shape in _shapes():
        own = inspect.cleandoc(shape.__dict__.get("__doc__") or "")
        out += ["", f"### `{shape.__name__}`", *(["", own] if own else [])]
        if issubclass(shape, BaseModel):
            fields = ", ".join(f"{name}: {_type_name(f.annotation)}" for name, f in shape.model_fields.items())
            out += ["", f"Fields: {fields}."]
    out += ["", "## Guides", "", "`fl.guide(name)` prints one in `run`.", ""]
    out += [
        f"- [`{g.name}`](../mcp-server/src/fl_mcp/guides/{g.name}.md): {g.description}" for g in guides.load()
    ]
    return "\n".join(out) + "\n"


def _shapes() -> list[type]:
    """The types the SDK's calls return (its own classes and models), and the models
    inside them, in the order met."""
    found: list[type] = []
    pending = [signature(entry) for entries in AREAS.values() for entry in entries]
    while pending:
        for name in re.findall(r"\b[A-Z]\w+", pending.pop(0)):
            shape = next((getattr(m, name) for m in (schemas, contract, sdk) if hasattr(m, name)), None)
            ours = isinstance(shape, type) and (issubclass(shape, BaseModel) or shape.__module__ == sdk.__name__)
            if ours and shape not in (Note, sdk.FL, *found):
                found.append(shape)
                if issubclass(shape, BaseModel):
                    pending += [_type_name(f.annotation) for f in shape.model_fields.values()]
    return found


def _type_name(annotation: Any) -> str:
    if isinstance(annotation, type):
        return annotation.__name__
    return re.sub(r"\b(?:typing|fl_mcp(?:\.\w+)*)\.", "", str(annotation))


def _short(entry: str) -> str:
    """An index entry: the call without fl's prefix or optional arguments (``render(output, ...)``)."""
    return signature(entry, full=False).removeprefix("fl.")


def _doc(entry: str) -> str:
    obj = member(entry)
    doc = inspect.getdoc(obj.fget if isinstance(obj, property) else obj) or ""
    return doc


def main() -> None:
    print(reference_markdown(), end="")


if __name__ == "__main__":
    main()
