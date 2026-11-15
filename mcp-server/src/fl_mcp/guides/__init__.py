"""Guides: recipes and production craft the model opens with ``fl.guide(name)``.

A guide is one Markdown file in this folder, ``<name>.md``; adding the file is all
it takes. ``fl.guide()`` prints the index (each guide's name and description) and
the run tool's description lists the same index, generated from these files.

The format, which ``parse`` enforces and CI checks for every guide:

- Front matter first, between ``---`` lines, with exactly two keys, the ones SKILL.md
  (agentskills.io) uses, so the files can be published as client skills later; each
  value stays plain YAML (no ": " inside, no leading quote):
  ``name``, the file's stem in lowercase letters, digits and hyphens; and
  ``description``, one line of at most 80 characters saying what the guide is for
  (the run description lists it, so keep it short).
- The body: Markdown, one sentence per line, at most 6,000 characters. It holds the
  recipe or craft and the reasons for it, and names SDK methods (``fl.automate``)
  without restating their arguments or limits: ``help(fl.automate)`` owns those.
  It points to another guide instead of copying it.
- Exactly one fenced block (``` or ~~~), tagged ``python``: the worked example. CI runs it as a run cell against the SDK fakes (tests/sdk_fakes.py,
  ``StudioFake``) in an empty working directory, and checks that every ``fl.<name>``
  and ``theory.<name>`` the guide mentions exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DESCRIPTION_MAX_CHARS = 80
BODY_MAX_CHARS = 6_000
_FRONT_MATTER = re.compile(r"\A---\n(?P<head>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_FENCE = re.compile(r"^```(?P<lang>[^\n`]*)\n(?P<code>.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_FENCE_LINE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)


@dataclass(frozen=True)
class Guide:
    name: str
    description: str
    body: str
    example: str


def parse(text: str, stem: str) -> Guide:
    """One guide from its file's text; a file that breaks the format raises ValueError."""
    match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
    if match is None:
        raise ValueError(f"{stem}.md: starts with front matter between --- lines")
    pairs = [_key_value(line, stem) for line in match["head"].splitlines()]
    head = dict(pairs)
    if sorted(key for key, _ in pairs) != ["description", "name"]:
        raise ValueError(f"{stem}.md: front matter has exactly name and description, once each")
    if any(": " in value or value[:1] in "\"'" for value in head.values()):
        raise ValueError(f"{stem}.md: front matter values are plain YAML, with no ': ' and no leading quote")
    name, description, body = head["name"], head["description"], match["body"].strip()
    if name != stem or not NAME.fullmatch(name):
        raise ValueError(f"{stem}.md: name is the file's stem in lowercase letters, digits and hyphens")
    if not 0 < len(description) <= DESCRIPTION_MAX_CHARS:
        raise ValueError(f"{stem}.md: description is one line of 1 to {DESCRIPTION_MAX_CHARS} characters")
    if len(body) > BODY_MAX_CHARS:
        raise ValueError(f"{stem}.md: the body is {len(body)} characters, over {BODY_MAX_CHARS}")
    fences = _FENCE.findall(body)
    if [lang.strip() for lang, _ in fences] != ["python"] or len(_FENCE_LINE.findall(body)) != 2:
        raise ValueError(f"{stem}.md: exactly one fenced block, tagged python (the worked example)")
    return Guide(name, description, body, fences[0][1])


def load() -> list[Guide]:
    """Every guide shipped in this folder, by name."""
    folder = files(__name__)
    return sorted(
        (parse(entry.read_text(encoding="utf-8"), entry.name.removesuffix(".md"))
         for entry in folder.iterdir() if entry.name.endswith(".md")),
        key=lambda guide: guide.name,
    )


def index(guides: list[Guide]) -> str:
    """One line per guide: its name and what it is for."""
    width = max((len(g.name) for g in guides), default=0)
    return "\n".join(f"{g.name.ljust(width)}  {g.description}" for g in guides)


def _key_value(line: str, stem: str) -> tuple[str, str]:
    key, sep, value = line.partition(":")
    if not sep:
        raise ValueError(f"{stem}.md: front matter lines are key: value, got {line!r}")
    return key.strip(), value.strip()
