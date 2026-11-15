"""Guides (fl_mcp/guides): the recipes and craft the model opens with fl.guide(name).

The recipes used to be hand-written into the run description, checked against nothing.
Here every guide's worked example runs as a run cell on the SDK fakes and every fl name
it mentions must exist, so a guide fails CI the day the SDK changes under it.
"""

from __future__ import annotations

import re

import pytest
from sdk_fakes import StudioFake

from fl_mcp import errors, guides, sdk, theory
from fl_mcp.kernel.cell import new_namespace, run_cell
from fl_mcp.store import Store

GUIDES = guides.load()


def test_the_recipes_that_left_the_description_are_guides():
    assert {"listening", "revise", "sidechain", "sounds", "chops"} <= {g.name for g in GUIDES}


@pytest.mark.parametrize("guide", GUIDES, ids=lambda g: g.name)
def test_a_guide_names_only_real_sdk_members(guide):
    for name in re.findall(r"\bfl\.(\w+)", guide.body):
        assert hasattr(sdk.FL, name) and not name.startswith("_"), f"{guide.name}: fl.{name}"
    for name in re.findall(r"\btheory\.(\w+)", guide.body):
        assert callable(getattr(theory, name, None)), f"{guide.name}: theory.{name}"


@pytest.mark.parametrize("guide", GUIDES, ids=lambda g: g.name)
def test_a_guides_worked_example_runs_on_the_fakes(guide, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    fake = StudioFake(store=Store("guide", root=tmp_path / "store"), library=tmp_path / "library")

    result = run_cell(guide.example, new_namespace(sdk.connect(fake)))

    assert result.outcome == "ok", f"{guide.name}:\n{result.stdout}\n{result.traceback}"


def test_fl_guide_prints_the_index_then_one_guide(fake_session, capsys):
    fl = sdk.connect(fake_session)

    fl.guide()
    assert capsys.readouterr().out == guides.index(GUIDES) + "\n"
    fl.guide("sidechain")
    assert "fl.automate" in capsys.readouterr().out


def test_fl_guide_needs_no_fl_session(capsys):
    """Computer-use runs serve run with no FL session (--workspace-only), and the harness
    points those models to fl.guide() too (fl-studio-cua#27): guides read no session."""
    fl = sdk.FL(None)

    fl.guide()
    fl.guide("listening")
    out = capsys.readouterr().out
    assert guides.index(GUIDES) in out and "pyloudnorm" in out


def test_an_unknown_guide_names_the_real_ones(fake_session):
    with pytest.raises(errors.ToolError) as raised:
        sdk.connect(fake_session).guide("dubstep")
    assert raised.value.candidates == [g.name for g in GUIDES]


GOOD_HEAD = "---\nname: demo\ndescription: A demo.\n---\n"
EXAMPLE = "```python\nfl.tempo\n```\n"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("No front matter.\n" + EXAMPLE, "front matter"),
        ("---\nname: demo\ndescription: A demo.\ntitle: Demo\n---\n" + EXAMPLE, "exactly name and description"),
        ("---\nname: other\ndescription: A demo.\n---\n" + EXAMPLE, "stem"),
        ("---\nname: demo\ndescription: " + "x" * 81 + "\n---\n" + EXAMPLE, "description"),
        (GOOD_HEAD + "Prose only.\n", "exactly one fenced block"),
        (GOOD_HEAD + EXAMPLE + EXAMPLE, "exactly one fenced block"),
        (GOOD_HEAD + EXAMPLE + "```\nplain\n```\n", "exactly one fenced block"),
        (GOOD_HEAD + "x" * 6_001 + "\n" + EXAMPLE, "over 6000"),
        (GOOD_HEAD + EXAMPLE + "~~~\nplain\n~~~\n", "exactly one fenced block"),
        ("---\nname: demo\nname: demo\ndescription: A demo.\n---\n" + EXAMPLE, "once each"),
        ("---\nname: demo\ndescription: Demo: a test.\n---\n" + EXAMPLE, "plain YAML"),
    ],
)
def test_parse_refuses_a_guide_that_breaks_the_format(text, problem):
    with pytest.raises(ValueError, match=problem):
        guides.parse(text, "demo")


def test_parse_reads_the_example_out_of_a_good_guide():
    parsed = guides.parse(GOOD_HEAD + "Why.\n\n" + EXAMPLE, "demo")
    assert (parsed.name, parsed.description, parsed.example) == ("demo", "A demo.", "fl.tempo\n")
