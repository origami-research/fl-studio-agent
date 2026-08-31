"""One-command discovery: ``python -m fl_mcp.discover`` (docs/research r4 A3).

Everything a fresh FL build's discovery needs, sequenced into one resumable
driver, so a sightmesh task on the mac with FL wraps exactly this command.

    static scrape  ->  passes  ->  emit profile + diff

The STATIC half needs no FL: :mod:`fl_mcp.scrape_rtti` reads struct layouts, the
VMT offset and the dispatcher bound straight from FLEngine's own RTTI, and
:mod:`fl_mcp.passes.map_align` aligns that scraped jump table to the SDK command
map - so ``discover`` alone already answers "what changed on this build, and
which ids are genuinely new" without a live pass. That report is printed every
run; with ``--out`` it also writes the merged profile and diffs it against the
loaded baseline.

The LIVE half (``--live``) runs the ``wp_*`` passes over a G2 Transport that the
ORCHESTRATOR has already pointed at a running Bridge. This driver never launches
or drives FL: FL is a GUI app, the sweep is expected to take it down, and the
relaunch-on-crash supervision is a sightmesh job around this command, not code
here. See native/bridge/RUNBOOK_DISCOVERY.md for the orchestrator's live loop. On ``--resume``,
the :class:`~fl_mcp.core.discovery.FileLedger` replays finished passes and the
Bridge's own quarantine ledger skips the id a prior run crashed on, so a crash
strictly advances discovery; ``sweep`` folds each newly-learned fatal into the
profile's ``dispatcher.unsafe`` - no C++ edit for a new build's crashers.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import scrape_rtti
from .core.discovery import FileLedger, discover
from .passes import DEFAULT_PASSES, classify
from .profile import CapabilityProfile, load_profile, parse_profile, profile_to_dict

log = logging.getLogger(__name__)

LEDGER_FILENAME = "discovery_ledger.json"


def scrape_into(profile: CapabilityProfile, dylib: Path) -> CapabilityProfile:
    """The static phase: replace the scraper-owned blocks of ``profile`` with a
    fresh read of ``dylib`` (structs, VMT offset, dispatcher addresses/bound).

    Live-probe-owned blocks (unsafe ids, getters, capabilities, tick) are
    carried through untouched - the live passes own those. Raises ScrapeError
    if the image does not carry the RTTI we expect, never a thinned profile.
    """
    from datetime import datetime, timezone

    result = scrape_rtti.scrape(dylib)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = scrape_rtti.merge_scrape(profile_to_dict(profile), result, generated)
    return parse_profile(merged)


def alignment_report(profile: CapabilityProfile) -> str:
    """The human line the run always prints: what the scraped table looks like
    against the SDK map, and the ids the live sweep would actually probe."""
    align = classify(profile)
    from .passes import unknown_ids

    unknowns = unknown_ids(profile)
    lines = [
        f"dispatcher aligned to SDK map: {len(align.matched)} matched, "
        f"{len(align.moved)} moved, {len(align.new)} new",
    ]
    if align.moved:
        moved = ", ".join(f"{i} ({align.name(i)})" for i in align.moved)
        lines.append(f"  MOVED (documented id now inert - a handler disappeared): {moved}")
    lines.append(f"  sweep input (genuinely-new, not yet explained): {unknowns or 'none'}")
    return "\n".join(lines)


def diff_report(baseline: CapabilityProfile, current: CapabilityProfile) -> str:
    """The deliverable a human reads: what changed between two profiles.

    Compares the blocks that carry meaning - the dispatcher surface, struct
    layouts, getters, capabilities - and reports each field-level change by
    name. An empty diff prints one line; that is itself the answer ("nothing
    moved on this build")."""
    old, new = profile_to_dict(baseline), profile_to_dict(current)
    changes: list[str] = []
    for block in ("meta", "dispatcher", "structs", "getters", "capabilities", "tick", "flp", "fst"):
        _diff_into(changes, block, old.get(block), new.get(block))
    if not changes:
        return "diff vs baseline: no change"
    return "diff vs baseline:\n" + "\n".join(f"  {c}" for c in changes)


def _diff_into(changes: list[str], path: str, old: object, new: object) -> None:
    """Recurse into dict/list structure, appending 'path: old -> new' for leaves."""
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            _diff_into(changes, f"{path}.{key}", old.get(key), new.get(key))
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (o, n) in enumerate(zip(old, new)):
            _diff_into(changes, f"{path}[{i}]", o, n)
    else:
        changes.append(f"{path}: {old!r} -> {new!r}")


def run(
    profile: CapabilityProfile,
    dylib: Path | None,
    *,
    live: bool,
    ledger_dir: Path | None,
) -> CapabilityProfile:
    """The whole pipeline, transport-free where it can be. Returns the merged profile."""
    if dylib is not None:
        profile = scrape_into(profile, dylib)
    print(alignment_report(profile), file=sys.stderr)
    if not live:
        return profile
    transport = _build_transport()
    ledger = FileLedger((ledger_dir or Path.cwd()) / LEDGER_FILENAME) if ledger_dir else None
    return discover(list(DEFAULT_PASSES), transport, profile, ledger=ledger)


def _build_transport():
    """Bind a live G2 Transport (integration seam). Lazy: the static half needs
    no transport, and this lane builds/tests without G2 merged. When the live
    path is asked for and G2 is absent, fail with the honest reason."""
    try:
        from .core.carriers.spool import SpoolCarrier  # type: ignore[attr-defined]
        from .core.transport import Transport
    except ImportError as exc:  # pragma: no cover - exercised only post-integration
        raise SystemExit(
            f"live discovery needs the G2 transport ({exc}); run without --live for the "
            "static scrape + alignment report, or wait for core.transport to land"
        ) from exc
    return Transport([SpoolCarrier()])  # pragma: no cover - real carriers need FL


def _default_dylib() -> Path:
    from .fl.paths import fl_app

    return fl_app() / "Contents" / "Libs" / "FLEngine_x64.dylib"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m fl_mcp.discover",
        description="Scrape FL's ABI, align the dispatcher, emit a capability profile + diff.",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the merged profile here")
    parser.add_argument("--dylib", type=Path, default=None, help="FLEngine_x64.dylib (default: installed FL)")
    parser.add_argument("--no-scrape", action="store_true", help="skip the static scrape; align the loaded profile as-is")
    parser.add_argument("--live", action="store_true", help="run the wp_* passes over a live G2 Transport (orchestrator only)")
    parser.add_argument("--resume", action="store_true", help="reuse the discovery ledger next to --out (crash-resume)")
    args = parser.parse_args(argv)

    baseline = load_profile()
    dylib = None if args.no_scrape else (args.dylib or _default_dylib())
    if dylib is not None and not dylib.is_file():
        print(f"error: no dylib at {dylib} (use --no-scrape to align the loaded profile)", file=sys.stderr)
        return 1

    ledger_dir = args.out.parent if (args.resume and args.out is not None) else None
    try:
        merged = run(baseline, dylib, live=args.live, ledger_dir=ledger_dir)
    except scrape_rtti.ScrapeError as exc:
        print(f"error: scrape failed: {exc}", file=sys.stderr)
        return 1

    print(diff_report(baseline, merged), file=sys.stderr)
    if args.out is not None:
        args.out.write_text(json.dumps(profile_to_dict(merged), indent=2) + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
