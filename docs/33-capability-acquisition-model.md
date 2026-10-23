# Capability acquisition model

This is the durable routing rule for acquiring every new FL capability.

For any proposed capability, first classify it as project data or a live host call, then extend the matching reusable engine. Do not describe a host-call capability as hard, different, or bespoke.

## 1. Project data: the `.flp` file-RE engine

Use the `flpkit` file-RE engine when the capability is represented in the `.flp`, including notes, playlist clips, automation, levels, mixer routing, plugin/channel state, and future file-carried lifecycle operations.

Acquire the format through FL-authored minimal pairs, byte diffs, an encoder and decoder, and save/reparse verification. The resulting rule belongs in the reusable codec as format data, not in capability-specific reverse engineering.

## 2. Live host calls: the discovery pipeline

Use the discovery pipeline when FL performs the capability through a live host call.

The pipeline combines the RTTI scraper, `core/discovery.py`, profile facts, and reusable passes such as `map_align`, `getters`, `target`, and `sweep`. It aligns FL's dispatcher jump table with SDK `FHD_*` host-call names and classifies each id as matched, moved, new, unsafe, or refuted.

This pipeline found render limits and the note/parameter paths, and it remains the engine for future host-call capabilities. Add a pass when new evidence is needed; do not hand-decode a capability-specific path.

The only manual step is a bounded live sweep of unknown candidate dispatcher ids after static safety classification, always honoring `native/abi/fl_dispatch_ledger.h`. That sweep establishes evidence for the reusable pipeline; it is never per-capability reverse engineering.

## 3. Runtime shape

Every acquired capability flows through one of the generic pipelines:

| Pipeline | Purpose |
|---|---|
| Codec | `.flp` locate -> decode/encode -> splice -> verify-by-readback |
| Transport | frame -> send -> await/drain over the best live carrier |
| Router | operation + evidence -> first serving lane, breaker-gated |
| Discovery | pass spec -> commands -> evidence -> profile update |
| Provenance | machine-readable tag recording how and when a capability was verified |

Specificity belongs in the feature file or pass spec. Genericness belongs in the pipeline. A new capability should usually add data, a schema, a pass, or a tag; needing a new bespoke path is evidence that the pipeline is wrong.

## Correction recorded 2026-08-31

Effects, instruments, and presets were wrongly framed as a separate manual-RE wall.

They are either `.flp` project data or further host calls discoverable through the engines above, not bespoke hand-decoding.

When a new capability arrives, classify it first, route it to one of these two engines, and extend that engine only as reusable data or a pass.

Related: [coverage map](24-coverage-map.md) and [reading guide](READING_GUIDE.md).
