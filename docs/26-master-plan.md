# Master plan: report + phased implementation (2026-08-30)

Synthesis of four research streams into one dependency-ordered plan. Detail lives in `docs/research/r1..r4`; this is the executable order. Doctrine (#54) binds all of it: one canonical path, capability detection not flags, boundaries verify by readback, net-negative PRs, and every format constant derived/detected - hardcoded only as a warned fallback.

## The report: where we are

We have PROVEN, live, against real FL 2026: notes write with no reload to any PATTERN (PatNum honored); FL push-notifies transport/selection/focus/context/routing; the read surface (position/tempo/PPQ/selection) is free to poll (~0ms vs 8.7ms tick). We have PROVEN NOT possible: live note readback (no getter), live channel targeting (ChanNum never read, no channel object), live plugin instantiation (modal-only). The gap is not knowledge - it is that these verdicts sit behind an off-by-default flag instead of shipped.

Three root problems this plan fixes:
1. **Discovery is manual and its results are hardcoded** into five source files. Fix: a capability PROFILE emitted by a one-command discovery job; the runtime loads it; the RTTI scraper derives struct layouts per-install. A new FL version becomes "re-run discovery", not "re-edit source".
2. **Capabilities are gated, not shipped.** Fix: delete the flag; the router picks the best lane per call from evidence.
3. **Code-health debt**: session.py god-object, the throwaway probe still in-tree, hardcoded version-specific constants, brittle UI-coordinate clicking, dead second UDP carrier.

## Guiding invariants (from R4, now doctrine)
- **Detect-don't-assume**: every format constant is derived (file header / binary RTTI / profile) or detected by an in-data invariant (the `pattern_base==20480` stride model), with a hardcoded value only as a logged fallback.
- **No flags**: the best available lane is chosen automatically per operation. Bridge when its heartbeat is fresh, compatible, and the write's channel constraint is satisfiable; file lane otherwise and always for channel targeting + offline. The file lane is a first-class lane, not a hedge.
- **Boundaries verify**: receipts are readback, never echo, on every lane.

## Phased plan (dependency-ordered)

### Phase A - clean the foundation (behavior-preserving, unblocks everything)
- **A1. Split session.py into a `session/` package** (facade + link + lifecycle + router + notes + controls + state), public seam byte-identical; move `NotePlaneUnavailable`/`WriteRejected` to errors.py; split the test file along the same seams, no assertion changes. [R1 §1]
- **A2. Scope the dangerous shell + unify process detection**: `pkill` targets our own pid (not `-f` substring); `fl_process_running()` = one `pgrep -x` with the executable name derived from `fl_app()` Info.plist. [R4 B2-3, B2-4]
- **A3. Kill FL_MCP_BRIDGE -> capability detection**: `router.detect() -> Capabilities` (heartbeat freshness + `proto=`/`pat=`/`chan=` line + ping round-trip), per-call fallback with a `BridgeBreaker` (closed/open/half_open). Bridge eligible for a note write iff capable AND `chan==target.channel`; else file lane. [R1 §2-3, R2 §2, R4 P0]
- **A4. UDP carrier decision**: ADOPT it (session tries `bridge_wire.discover()`, uses UDP when fresh, spool otherwise - capability detection) rather than delete, because the observer needs it. One canonical command path. [R4 B3-1]

### Phase B - ship the proven live capabilities
- **B1. Bulkhead + promote the bridge to default note lane.** Complete the in-process crash-isolation invariant set (static fatal-id ledger moved to `native/abi/`, claim-before-execute, concrete-targets-only, bounded tick work, build-time struct asserts); surface the SetCurrentPattern side effect as intended. Delete the 396-line pyscript note lane. [R2 Phase 0-1]
- **B2. The observer / user-action stream.** Bridge forwards FL's push callbacks (0-latency) + lazy poll-and-diff at checkpoints (before-action / after-tool / at-save) for score edits; event schema over UDP; server surfaces a user-action stream to the model; agent-vs-human concurrency rules (never clobber a mid-edit note). Reads are lazy on-demand, not forced realtime. [R2 Phase 2, R4 Half A]
- **B3. Live reads via confirmed getters** (position/tempo/PPQ/selection/playing/focus); retire the MIDI wire's read plane (delete `state.snapshot` composite); note-content reads stay file-based. [R2 Phase 3]

### Phase C - portability + self-reconfiguration (do FL-version-proofing)
- **C1. Capability profile artifact + loader** (`profile.py`, `<spool>/profile.json`, packaged `profiles/fl-26.1.5-5384-arm64.json`); migrate the version-specific constants (struct layouts, dispatcher bound, unsafe ids, VMT offset, spool roots, .fst facts) into it; native side via a codegen'd header so a wrong layout fails the BUILD. [R4 A1]
- **C2. RTTI scraper (#63)** emits struct/VMT/dispatcher-bound into the profile; golden test (would have caught Vol-is-Single) + drift alarm. [R4 A2]
- **C3. One-command discovery** (`python -m fl_mcp.discover`), a sightmesh job: static scrape -> provision -> launch+activate -> live passes (driver scripts transport/save itself) -> crash-resume supervisor -> emit profile + report + diff-vs-previous. Fold the probe PASSES into the Bridge; DELETE `native/probe/` (~2,600 LOC). [R4 A3, #80]
- **C4. flpkit detect-don't-assume**: event-size-override detection, cut-window template invariant, note-flags templating (upstream PRs). [R4 A4]

### Phase D - the production layer (Tier-2, needs new RE)
- **D1. Automation WRITE** (care rank 1): reader is 90% done; four named minimal-pair experiments crack the 112-byte trailer + target-link; `flpkit.write_automation` (patch existing clip) then `create_automation_clip`. [R3 §1]
- **D2. Mixer** via `FHD_SetContextInfo` (track name/color/volume/pan/mute-solo) live + routing via file RE; write-side probe as `wp_*` ops in the Bridge executor. [R3 §2]
- **D3. Instruments/presets**: no live instantiation API (modal-only) - template-carried + file-lane preset CARRY (copy vendor blob verbatim, template rule). Honest: no live "add a synth". [R3 §3]
- **D4. Audio clips** (T3): sampler-channel writes; `FHD_LoadAudioClip` (id 42) candidate. [R3 §4]

### Continuous - enablers
- **Fix CI** (owner lever): restores the automated gate; the RTTI drift tests become merge-gate tests. [#64]
- **Readability batch** rides along with A1: write/clear fold, `_ensure_hello` split, `GENERATOR_SLOT`, `SessionState.from_wire`. [R4 B4]
- **Schema trims**: delete `Channel.melodic`, forward `Track.pattern` (unlocks SDK pattern targeting), `current_pattern: int|None`. [R4 B3]
- **Delete `tools/fl_harness.py` UI-clicking** (configure_midi supersedes it), salvage `crash_logs_since`. [R4 B2-1]
- Retire pyflp after automation-write; delete `native/probe` in C3.

## Net line impact
Roughly **-4,000 to -4,500 lines** across the plan (probe deletion ~-2,600, pyscript lane ~-400, harness UI ~-170, dead carrier if not adopted, schema trims), against ~+1,000 of profile/scraper/observer. The tree ends smaller and every version-specific fact has ONE home.

## Ordering rationale
A before B: don't build the observer on a god-object or behind a flag. C can start in parallel with B (the scraper has no dependency) but its profile MIGRATION lands after A's router exists. D is last: it needs new live RE and rides the bulkhead + discovery harness from B/C.
