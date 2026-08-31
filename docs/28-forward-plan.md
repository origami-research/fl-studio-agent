# Forward plan: parallel lanes for the remaining work + new ideas

State: the parallel rebuild landed (PR #91) - flag-free router, session package, capability profile + RTTI scraper, observer, bridge bulkhead, probe deleted. V3 live-verified: heartbeat signals, event forwarding, bridge-default writes (backend=bridge), bulkhead survives the fatal ids. This plan covers what's left, as maximally-parallel lanes against frozen surfaces, plus new ideas that extend our proven patterns.

## New ideas (each extends a pattern that already paid off)

1. **Auto-aligning command mapper** (extends: RTTI scraper + detect-don't-assume). FL's dispatcher is an ORDERED jump table with names we know from the SDK header. Derive the map: scrape the table, align each id to its expected name by structure, classify matched/moved/new. The risky live sweep shrinks to ONLY the unknowns. A new FL version costs near-zero crashes. (#63)
2. **One-command discovery as a local subprocess** (extends: reproducible discovery). The live loop can't be a sightmesh worker (agents never drive FL), so make it a scripted subprocess: static scrape -> provision -> launch+activate -> passes -> crash-resume -> emit profile + diff. Removes the manual driving. (R4 A3)
3. **Self-updating profile via CI-on-FL-install** (extends: capability profile + the drift alarm). Any machine with a new FL auto-runs discovery, diffs the profile, opens a PR with the delta. Version-proofing becomes automatic, not a supervised event.
4. **End-to-end capability regression suite** (extends: E2E matrix + corpus differential). One golden test runs compose -> arrange -> sound-design -> render and asserts the output - the top-level "does the product still work" gate, run per FL version. Catches integration rot the unit tests miss.
5. **Provenance/confidence tags on every capability** (extends: the verification stack + recording falsified beliefs). Each capability carries how+when it was verified (live / byte-invariant / oracle) and its evidence. The model sees solid-vs-experimental; a stale tag (old FL version) auto-flags for re-verification. The coverage map becomes machine-checkable.
6. **Property-based round-trip fuzzing of the writers** (extends: byte-level invariants). Generate random valid notes/clips/automation, write -> read -> assert identity. Catches edge cases the fixed 164-file corpus never contains.
7. **Auto-minimal-pair RE harness** (extends: the RE playbook). The human authors ONE change in FL; the harness auto-localizes the changed bytes and proposes the field type. Shrinks manual RE to a single label - the semantic floor, minimized.
8. **Dogfood corpus** (extends: the dogfooding that caught target-blindness). Accumulate agent-authored projects as regression fixtures, so we test our OWN outputs, not just FL's demos.

## The lanes (disjoint ownership, frozen shared surfaces)

Shared surfaces (already frozen or defined here): capability profile (docs/27 §4), discovery command set (wp_* ops), the automation API (flpkit write_automation), the provenance-tag schema (§ below), the E2E harness seam.

| Lane | Owns | Delivers |
|---|---|---|
| **LA discovery-auto** | `fl_mcp/discover.py`, `scrape_rtti.py` (mapper), `profiles/` updater, discovery tests | ideas 1+2+3: auto-aligning mapper, one-command subprocess driver, profile auto-update + diff |
| **LB bridge-final** | `session/facade.py` (pre-warm), `native/abi/fl_dispatch_ledger.h` (id 106), `observer.py` (poll-and-diff finish), a new `capability_registry.py` | bridge pre-warm (first write uses bridge), id 106 quarantined, observer score-edit poll-and-diff + concurrency guard verified, idea 5: provenance tags |
| **LC automation-write** | `flp.py`+`session/controls.py` (set_automation adoption), `experiments/automation_re/` | adopt flpkit 0.5.0 write_automation; the live minimal-pair RE scaffold (4 experiments) to unlock create_automation_clip |
| **LD tier2-production** | `native/bridge/` wp_* mixer ops, `docs/research/` write-side plan | mixer via FHD_SetContextInfo (track name/color/vol/pan/mute) as wp_ ops; honest instrument/preset feasibility; routing RE plan |
| **LE quality-future** | `tests/e2e/` (regression suite), `tools/fuzz/`, `tools/minimal_pair/`, dogfood fixtures | ideas 4+6+7+8: E2E capability regression, property/fuzz round-trip, auto-minimal-pair harness, dogfood corpus |

CI restoration is the OWNER lever (org billing) - not a lane, but it's what makes LE's gates run automatically.

## Provenance-tag schema (LB publishes; the coverage map + model consume)
```python
@dataclass(frozen=True)
class CapabilityTag:
    capability: str                 # "notes.write.pattern", "automation.write", ...
    status: Literal["live","byte","oracle","planned"]
    verified_fl: str | None         # FL version last confirmed against
    evidence: str                   # runbook/verdict/test ref
    stale: bool                     # verified_fl != current profile.fl_version
```

## Dependencies / order
LA, LC, LD, LE are independent (disjoint files, no cross-imports). LB's provenance tags are consumed by LE's regression suite and the coverage map, so LB's tag schema is a frozen surface LE codes against (Protocol, no hard import). LA's profile diff feeds idea 3's CI job (owner). Automation live-RE (LC) and mixer live-RE (LD) both need an orchestrator FL window - each ships an offline scaffold + a runbook; the live rungs are the orchestrator's.

## Net posture
Every lane is net-neutral-to-negative or additive-with-tests. The theme continues: derive-don't-assume, prove-offline-first, delete-the-superseded, one-canonical-path, record-what-we-learn. The end state is a system that RE-maps itself on any FL version, ships every proven capability with no flags, tags each capability with how-we-know-it-works, and regression-guards the whole compose-to-render loop.
