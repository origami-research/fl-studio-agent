# Capability acquisition model

This doctrine is the authoritative routing rule for acquiring every new FL capability.

For any proposed capability, first classify it as project data or a live host call, then use its matching reusable engine.

Do not describe a host-call capability as hard, different, or bespoke.

## 1. Project data: the `.flp` file-RE engine

Use the `flpkit` file-RE engine when the capability is represented in the `.flp`, including notes, automation, levels, and mixer routing.

Acquire the format through FL-authored minimal pairs, byte diffs, an encoder and decoder, and save/reparse verification.

The resulting format rule belongs in the reusable codec, not in capability-specific reverse engineering.

## 2. Live host calls: the discovery pipeline

Use the discovery pipeline when FL performs the capability through a live host call.

The pipeline combines `scrape_rtti.py`, `core/discovery.py`, and reusable `passes/` such as `map_align`, `getters`, `target`, and `sweep`.

It aligns FL's dispatcher jump table with SDK `FHD_*` host-call names and classifies each id as matched, moved, or new.

This pipeline found render and the note and parameter calls, and it is the engine for future host-call capabilities.

Add a pass such as `passes/plugins.py` when plugins need new evidence; do not hand-decode a capability-specific path.

The only manual step is the one-time live sweep of unknown candidate dispatcher ids, always honoring `native/abi/fl_dispatch_ledger.h`.

That sweep establishes evidence for the reusable pipeline; it is never per-capability reverse engineering.

## Correction recorded 2026-08-31

Effects, instruments, and presets were wrongly framed as a separate manual-RE wall.

They are further host calls and are discoverable through the discovery pipeline, not bespoke hand-decoding.

When a new capability arrives, classify it first, route it to one of these two engines, and extend that engine only as reusable data or a pass.

Related: [coverage map](24-coverage-map.md) and [generic pipelines](29-generic-pipelines.md).
