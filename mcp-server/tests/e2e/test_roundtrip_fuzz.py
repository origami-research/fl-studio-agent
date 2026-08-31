"""The generic codec property suite: random valid data through every format.

ONE driver, formats as ROWS of data (docs/29 §2: test the pipeline once and
every format is covered; a new format is a new row, never a new test). Per
row and seed, against a real FL-written template:

  patch -> independent re-read -> identity   (the codec's readback receipt
      must equal what a fresh ``read`` decodes - two paths, one truth)
  patch the SAME data again -> byte fixpoint (a second identical replace
      write must change nothing: catches encode/decode asymmetry)

plus a cross-format pass: every row writes to the same file and every view
must still match its receipt afterwards (no format clobbers another).

Headless tier 1 - runs wherever FL's bundled Data dir exists. Deeper soak:
``FL_FUZZ_SEEDS=1,2,...,200 pytest tests/e2e/test_roundtrip_fuzz.py``.
"""

from __future__ import annotations

import os
import random
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flpkit as flp
import pytest
from tools.fuzz import gen_clips, gen_levels, gen_notes, gen_points, gen_tempo

from e2e.matrix import MATRIX, _pattern_ids, requires_fl_data

SEEDS = [int(s) for s in os.environ.get("FL_FUZZ_SEEDS", "1,2,3").split(",") if s.strip()]


@dataclass(frozen=True)
class Ctx:
    """One tmp copy of the template plus the write target every row shares."""

    path: Path
    fresh_pattern: int
    channel: int
    patterns: tuple[int, ...]  # patterns that exist, for clip generation
    automation_channel: int  # an existing kind-5 channel whose blob we patch


@dataclass(frozen=True)
class FuzzRow:
    """One format under fuzz. ``apply`` patches random elements and returns
    the codec's receipt; ``view`` re-reads the same elements independently
    (None when the receipt IS the only decoded view, e.g. levels)."""

    name: str
    apply: Callable[[Ctx, random.Random], Any]
    view: Callable[[Ctx], Any] | None


def _make_ctx(tmp_path: Path) -> Ctx:
    # The one matrix project carrying every row's element, automation included.
    source = next(p.source for p in MATRIX if p.id == "demo-80-stride-automation")
    if source is None:
        pytest.fail("demo-80-stride-automation template missing from the FL corpus - re-pin the matrix")
    target = tmp_path / source.name
    shutil.copy2(source, target)
    target.chmod(0o644)
    project = flp.read(target)
    return Ctx(
        path=target,
        fresh_pattern=max(_pattern_ids(target.read_bytes()), default=0) + 1,
        channel=next(c.index for c in project.channels if not c.is_automation),
        patterns=tuple(sorted({item.pattern for item in project.playlist if item.pattern is not None})),
        automation_channel=next(c.index for c in project.channels if c.is_automation),
    )


def _notes_apply(ctx: Ctx, rng: random.Random) -> list[flp.FlpNote]:
    receipt = flp.write_notes(
        ctx.path, gen_notes(rng), pattern=ctx.fresh_pattern, channel=ctx.channel, mode="replace"
    )
    return sorted(receipt, key=lambda n: (n.position, n.key))


def _notes_view(ctx: Ctx) -> list[flp.FlpNote]:
    return sorted(flp.read(ctx.path).notes_in(ctx.fresh_pattern), key=lambda n: (n.position, n.key))


def _clips_apply(ctx: Ctx, rng: random.Random) -> list[flp.FlpPlaylistItem]:
    return flp.write_playlist(ctx.path, gen_clips(rng, ctx.patterns), mode="replace")


def _clips_view(ctx: Ctx) -> list[flp.FlpPlaylistItem]:
    return [item for item in flp.read(ctx.path).playlist if item.arrangement == 0]


def _points_apply(ctx: Ctx, rng: random.Random) -> list[Any]:
    from flpkit import codec
    from flpkit.formats import AutomationFormat

    flp.write_automation(ctx.path, ctx.automation_channel, gen_points(rng), mode="replace")
    return codec.read(ctx.path, AutomationFormat(), codec.Target(channel=ctx.automation_channel))


ROWS = (
    FuzzRow("notes", _notes_apply, _notes_view),
    FuzzRow("playlist", _clips_apply, _clips_view),
    FuzzRow("automation", _points_apply, None),
    FuzzRow("levels", lambda ctx, rng: flp.set_channel_levels(ctx.path, ctx.channel, **gen_levels(rng)), None),
    FuzzRow("tempo", lambda ctx, rng: flp.set_tempo(ctx.path, gen_tempo(rng)), lambda ctx: flp.read(ctx.path).tempo),
)


@requires_fl_data
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("row", ROWS, ids=[r.name for r in ROWS])
def test_patch_read_identity_and_fixpoint(row, seed, tmp_path):
    ctx = _make_ctx(tmp_path)

    receipt = row.apply(ctx, random.Random(seed))
    if row.view is not None:
        assert row.view(ctx) == receipt, f"[{row.name}] independent re-read disagrees with the write receipt"

    first = ctx.path.read_bytes()
    again = row.apply(ctx, random.Random(seed))  # same seed -> identical elements
    assert again == receipt, f"[{row.name}] second identical write returned a different receipt"
    assert ctx.path.read_bytes() == first, f"[{row.name}] second identical write changed bytes (no fixpoint)"


@requires_fl_data
@pytest.mark.parametrize("seed", SEEDS)
def test_formats_do_not_clobber_each_other(seed, tmp_path):
    ctx = _make_ctx(tmp_path)
    receipts = {row.name: row.apply(ctx, random.Random(seed)) for row in ROWS}
    for row in ROWS:
        if row.view is not None:
            assert row.view(ctx) == receipts[row.name], (
                f"[{row.name}] view changed after later formats wrote - a writer is not surgical"
            )
