"""Every text the engine shows a model holds to docs/model-text-standard.md.

The model reads the run description (in each mode the engine serves), the server
instructions, every docstring help() shows, the guides, the starting project's notes
and the errors. These tests check the standard's structural rules over all of them at
once, so a new member, guide, error or mode is covered the day it is added:

- R3 generated: the numbers, the pre-bound names and the package list come from the code
  and the host, and the result fields and outcomes the description names are the code's;
- R4 name only what exists: every fl, theory, track and effect member named exists;
- R5 true in every mode: each mode's description is rendered and fits the clients' limit,
  and a guide's prose holds for any project, not only the engine's starting one;
- R13 minimal and model-facing: no engineering references (source files, design docs,
  dates, issue numbers) in what the model reads.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import re
from typing import get_args

import pytest
from sdk_fakes import STUDIO_BUSES, STUDIO_CHANNELS, STUDIO_SENDS
from test_clean_traces import _error_strings, _sdk_docs

from fl_mcp import flp, guides, manual, sdk, server, theory
from fl_mcp.contract import FLLicense, RunOutcome, RunResult
from fl_mcp.kernel.display import MAX_IMAGES
from fl_mcp.template import template_path

GUIDES = guides.load()
MODES = [
    (fl_license, confined)
    for fl_license in (FLLicense.LICENSED, FLLicense.TRIAL, None)
    for confined in (True, False)
]
DESCRIPTIONS = {
    f"run description ({fl_license.value if fl_license else 'no session'}, "
    f"{'confined' if confined else 'unconfined'})": server.run_description(
        fl_license=fl_license, confined=confined
    )
    for fl_license, confined in MODES
}
TEMPLATE = flp.project_guide(template_path(), [name for name, _ in STUDIO_CHANNELS])
TEMPLATE_NOTES = "\n".join([TEMPLATE.notes or "", *TEMPLATE.about.values()])

# Where each prefix a model writes points: fl.render, theory.chord, track.write, effect.params.
OWNERS = {"fl": sdk.FL, "theory": theory, "track": sdk.Track, "Track": sdk.Track,
          "effect": sdk.Effect, "Effect": sdk.Effect}
MEMBER = re.compile(r"\b(fl|theory|track|Track|effect|Effect)\.([A-Za-z_]\w*)")

# What a model has no use for and cannot open: source files, design docs, dates, issues.
ENGINEERING = re.compile(r"\bdocs/\d|\b\w+\.py\b|\b20\d\d-\d\d-\d\d\b|#\d{2,}|\bfl_mcp\.\w")


def _texts() -> dict[str, str]:
    """Every model-facing text, by where it comes from."""
    texts = dict(DESCRIPTIONS)
    texts["server instructions"] = server.server_instructions(with_fl=True)
    texts["server instructions (no session)"] = server.server_instructions(with_fl=False)
    texts.update((f"help({where})", doc) for where, doc in _sdk_docs())
    texts.update((f"help(theory.{name})", inspect.getdoc(fn) or "") for name, fn in _theory().items())
    texts.update((f"guide {g.name}", g.description + "\n" + g.body) for g in GUIDES)
    texts["starting project notes"] = TEMPLATE_NOTES
    return texts


def _theory() -> dict[str, object]:
    return {name: fn for name, fn in vars(theory).items()
            if inspect.isfunction(fn) and fn.__module__ == theory.__name__ and not name.startswith("_")}


def test_the_scan_sees_every_kind_of_text():
    texts = _texts()
    assert len(DESCRIPTIONS) == 6 and len(GUIDES) >= 6
    assert "help(FL.render)" in texts and "help(theory.drums)" in texts
    assert TEMPLATE.about and TEMPLATE.notes, "the starting project's notes must be read"


@pytest.mark.parametrize("where", sorted(_texts()))
def test_every_member_a_text_names_exists(where):
    """A renamed or removed member must not survive in any text: the harness prompt once
    told models fl could not add patterns long after fl.new_pattern shipped."""
    for owner, name in MEMBER.findall(_texts()[where]):
        assert not name.startswith("_") and hasattr(OWNERS[owner], name), f"{where}: {owner}.{name}"


def test_every_member_an_error_names_exists():
    strings = _error_strings()
    assert len(strings) > 50
    for where, text in strings:
        for owner, name in MEMBER.findall(text):
            assert hasattr(OWNERS[owner], name), f"{where}: {owner}.{name}"


def test_the_prose_check_catches_a_planted_name():
    names = {name for name, _ in STUDIO_CHANNELS}
    planted = 'Set `fl.track("Pad").set_levels(...)` on the Pad.'
    assert any(re.search(rf"(?<![\w.]){re.escape(n)}(?!\w)", planted) for n in names)


def test_the_check_catches_a_planted_member():
    assert [m for m in MEMBER.findall("use fl.export_offline or track.write") if not hasattr(OWNERS[m[0]], m[1])]


@pytest.mark.parametrize("where", sorted(_texts()))
def test_no_text_carries_engineering_references(where):
    found = ENGINEERING.search(_texts()[where])
    assert found is None, f"{where}: {found.group(0)!r}"


@pytest.mark.parametrize("where", sorted(DESCRIPTIONS))
def test_every_description_fits_the_clients_limit_with_room(where):
    """Claude Code cuts a description loaded upfront at 4,096 characters (and tool search
    reads only that much of a deferred one)."""
    assert len(DESCRIPTIONS[where]) < manual.DESCRIPTION_MAX_CHARS, len(DESCRIPTIONS[where])


def test_the_description_states_the_hosts_values():
    """No number in the description is written by hand: each is the code's or the host's."""
    loose = server.run_description(fl_license=None, confined=False, run_budget=123)
    tight = server.run_description(fl_license=None, confined=True, run_budget=123)
    assert "123 s limit" in loose and f"at most {MAX_IMAGES} per call" in loose
    assert manual.CONFINED in tight and manual.CONFINED not in loose


def test_the_result_fields_and_outcomes_the_description_names_are_the_codes():
    """What run returns is described by hand, so it is checked against RunResult."""
    returns = re.search(r"Returns \{(.+?)\}", manual.KERNEL.replace("{{", "{").replace("}}", "}")).group(1)
    assert set(re.findall(r"\w+", returns)) == set(RunResult.model_fields)
    outcomes = re.search(r"outcome is (.+?)\.\n", manual.KERNEL).group(1)
    named = {word for word in re.findall(r"\b(\w+)(?: \(|,| or |$)", outcomes)}
    assert set(get_args(RunOutcome)) <= named, named


def test_the_packages_the_description_names_are_installed_with_the_engine():
    """The description promises these imports; each must be a dependency, not a hope."""
    declared = {re.split(r"[\s<>=!~;\[]", req, maxsplit=1)[0].lower()
                for req in importlib.metadata.requires("fl-studio-agent") or []}
    for package in manual.INSTALLED:
        assert package.lower() in declared, package


@pytest.mark.parametrize("guide", GUIDES, ids=lambda g: g.name)
def test_a_guides_prose_holds_for_any_project(guide):
    """A model may start from the user's own project (hybrid and MCP runs take one), so a
    guide's prose names no channel or mixer track of the engine's starting project; that
    project's notes describe it. The worked example may use them: it runs on that project."""
    prose = guide.description + "\n" + re.sub(r"```.*?```", "", guide.body, flags=re.DOTALL)
    names = {name for name, _ in STUDIO_CHANNELS} | set(STUDIO_SENDS.values()) | set(STUDIO_BUSES.values())
    for name in sorted(names):
        found = re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", prose)
        assert found is None, f"{guide.name} names the starting project's {name!r}"
