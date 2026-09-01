# Master plan

This is the current executable plan. Older dated plan docs were collapsed into this page plus the living [coverage map](24-coverage-map.md) and the durable [capability acquisition model](33-capability-acquisition-model.md).

## Current state

The generic foundation has landed: flag-free routing, session package split, capability profile, RTTI scraper, observer, bridge bulkhead, UDP/spool transport, and provenance tags. The remaining work is no longer "find the architecture"; it is to finish the highest-care capabilities through the reusable engines.

Confirmed shape:

- Bridge writes selected-channel notes live and can target any pattern through `PatNum`; channel targeting stays file-lane because `ChanNum` is ignored and no channel object is available.
- Note-content reads stay file-based; host calls do not provide a note readback getter.
- FL push callbacks cover transport, position, focus/selection, context, and routing. Score edits require poll-and-diff.
- Existing automation-clip writes are file-lane and save/reparse verified. Creating a new automation clip with a target link still needs an FL-authored minimal pair.
- Mixer routing, effect chains, instruments, presets, and samples remain Tier 2/3 gaps tracked in docs/24.

## Invariants

- Classify every new capability through docs/33 before choosing work.
- Ship one canonical path and delete the superseded one in the same PR.
- Choose lanes by capability evidence, never by feature flags.
- Verify boundaries by readback: live FL when possible, then byte invariants, then oracle.
- Keep version-specific facts in the profile or derived from data. Hardcoded fallback values must be warned fallbacks.
- Keep each capability readable through the [reading guide](READING_GUIDE.md): one feature file plus one pipeline.

## Work queue

| Priority | Work | Engine | Gate |
|---|---|---|---|
| P0 | Bridge pre-warm and V3 live promotion | Router + transport + bridge | first write uses bridge when eligible; fatal ids stay quarantined |
| P1 | Observer score-edit poll-and-diff and concurrency guard | Observer + codec | user edits on the same target refuse or defer agent writes |
| P1 | Capability provenance coverage | Provenance | every public capability has a current tag; stale tags fail the gate |
| P2 | One-command discovery and profile auto-update | Discovery | static scrape -> live passes -> profile/report/diff, crash-resumable |
| P2 | Automation new-clip creation | Codec + RE harness | FL-authored one-action pair, writer, save/reparse equality |
| P2 | Mixer routing and effect-chain evidence | Codec or discovery, chosen by docs/33 | checked-in baseline/changed pair or safe host-call verdict |
| P3 | Instruments, presets, samples, lifecycle operations | Codec or discovery, chosen by docs/33 | no public writer until evidence reaches the docs/23 ladder |

CI restoration remains an owner lever. Until CI is healthy, local `ruff`, unit tests, and relevant e2e/live gates are the merge bar.

## Canonical doc set

- [docs/23](23-flp-reverse-engineering-playbook.md): file-format evidence ladder.
- [docs/24](24-coverage-map.md): living coverage/gap table and priority order.
- [docs/25 capability catalog](25-fl-capability-catalog.md): cross-surface evidence catalog.
- [docs/26](26-master-plan.md): this current execution queue.
- [docs/27](27-shared-surfaces.md): stable contracts still cited by code/tests.
- [docs/29](29-generic-pipelines.md): generic pipeline architecture.
- [docs/30](30-surfaces-and-readability.md) and [docs/READING_GUIDE](READING_GUIDE.md): readability contract and navigation.
- [docs/33](33-capability-acquisition-model.md): acquisition/routing doctrine.
