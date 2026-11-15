"""The model's manual is generated from the SDK's docstrings and the guides (fl_mcp/manual.py).

Hand-written copies of the SDK drifted: a harness prompt said fl could not add patterns
long after fl.new_pattern shipped. And Claude Code cuts a tool description loaded upfront
at 4,096 characters, so the 6,568-character description lost everything from
fl.load_sample on. These tests keep the description whole, complete and true.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import anyio

from fl_mcp import guides, manual, sdk, server, theory

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "sdk-reference.md"
DESCRIPTION = server.run.__doc__


def test_the_run_description_fits_claude_codes_limit():
    assert len(DESCRIPTION) < manual.DESCRIPTION_MAX_CHARS, len(DESCRIPTION)
    assert server.run_description(with_fl=True) == DESCRIPTION


def _public(owner: object, prefix: str) -> set[str]:
    """The members the class defines for the model; pydantic's own (``model_config``, which it
    writes into a model's class) are its API, not the SDK's."""
    return {f"{prefix}.{name}" for name in vars(owner) if not name.startswith(("_", "model_"))}


def test_with_no_fl_session_run_describes_the_kernel_and_the_guides_only():
    """--workspace-only refuses every fl call but fl.guide(), so listing the SDK there
    would send the model to calls that fail."""
    alone = server.run_description(with_fl=False)
    assert len(alone) < manual.DESCRIPTION_MAX_CHARS
    assert manual.NO_SESSION in alone and "Installed: numpy" in alone and "display(" in alone
    assert manual.sdk_index() not in alone and "fl.scene()" not in alone and "help(fl." not in alone
    assert "fl.guide(" in alone and all(g.name in alone for g in guides.load())
    assert re.findall(r"\bfl\.(\w+)", alone) == ["guide"] * alone.count("fl.guide")


def test_each_server_mode_serves_its_own_description_and_instructions():
    for workspace_only in (False, True):
        made = server.create_server(workspace_only=workspace_only)
        (listed,) = anyio.run(made.list_tools)
        assert listed.description == server.run_description(with_fl=not workspace_only)
        assert made.instructions == server.server_instructions(with_fl=not workspace_only)
        assert len(made.instructions) < manual.DESCRIPTION_MAX_CHARS


def test_every_public_sdk_member_is_in_the_index_once():
    listed = [entry for entries in manual.AREAS.values() for entry in entries]
    theory_functions = {
        f"theory.{name}"
        for name, obj in vars(theory).items()
        if inspect.isfunction(obj) and obj.__module__ == theory.__name__ and not name.startswith("_")
    }
    public = (
        _public(sdk.FL, "fl") | _public(sdk.Track, "Track") | _public(sdk.Effect, "Effect")
        | theory_functions | {"Note"}
    )

    assert len(listed) == len(set(listed)), "an SDK member is listed twice"
    assert set(listed) == public, {"missing": public - set(listed), "not public": set(listed) - public}
    for entry in listed:
        assert manual._short(entry) in DESCRIPTION, entry


def test_every_fl_name_in_the_description_exists():
    for name in re.findall(r"\bfl\.(\w+)", DESCRIPTION):
        assert hasattr(sdk.FL, name) and not name.startswith("_"), name


def test_the_description_lists_every_guide():
    for guide in guides.load():
        assert f"{guide.name}  " in DESCRIPTION and guide.description in DESCRIPTION, guide.name


def test_checked_in_reference_is_generated_from_the_docstrings():
    assert REFERENCE.read_text() == manual.reference_markdown(), (
        "docs/sdk-reference.md is stale; regenerate: uv run python -m fl_mcp.manual > ../docs/sdk-reference.md"
    )
