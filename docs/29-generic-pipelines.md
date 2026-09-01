# Generic pipelines: target architecture (skeleton) + why + parallel plan

Goal (owner directive): implement the remaining work MINIMALLY - no duplicate code, everything through shared GENERIC pipelines, zero hardcoding or manual reconfiguration, super generic, super readable. A new capability should be DATA dropped onto an existing pipeline, never a new hand-rolled code path.

The [capability acquisition model](33-capability-acquisition-model.md) is the prior routing rule: first classify every capability as `.flp` data or a live host call, then use the corresponding reusable engine.

## 1. The problem with the current shape (why refactor)

The code is organized BY FEATURE, so the same shape is hand-rolled per feature:

- **Writers duplicate a shape.** flpkit `write_notes`, `write_playlist`, `write_automation`, `set_channel_levels` are four functions that all do: locate splice site -> build blob -> splice + fix chunk lengths -> verify by readback. Adding "mixer write" would be a fifth copy. (R4 B4-5 already flagged the 110-line duplication.)
- **Readers duplicate a shape.** flpkit `read` decoders + `scene.py`'s row/clip/automation extraction are two decode paths over the same events.
- **Transports are three parallel IO paths.** `spool.py`, `wire.py`, `bridge_wire.py` (UDP) each frame/send/await separately; the observer adds a fourth reader. (R4 B3-1: a whole dead carrier.)
- **Routing is note-specific.** `core/router.py` routes NOTES from capability evidence; automation/mixer writes would each need their own routing branch.
- **Discovery passes are hand-coded per pass.** getters/timing/target/sweep are separate C++ blocks; a mixer-probe is a fifth.
- **Version-specific constants still live in code** in several places (the profile migration, R4 B1, is partly done).

None of this is BAD code - it's un-GENERALIZED code. Every pending feature (automation-write, mixer, discovery-auto, provenance) would add another copy.

## 2. The insight: five shapes, not twenty features

Everything we do is an instance of one of five generic pipelines:

| Pipeline | The one shape | Today's N copies -> |
|---|---|---|
| **CODEC** | locate -> (decode \| build) -> splice -> verify-by-readback, driven by an element SCHEMA | notes/playlist/automation/levels writers + readers -> 1 codec + N schemas |
| **TRANSPORT** | frame -> send -> await/drain, over a carrier chosen by capability | spool + wire + udp + observer-read -> 1 Transport + carriers |
| **ROUTER** | Operation + target -> the best Lane by capability evidence | note routing -> 1 router over any Operation |
| **DISCOVERY** | Pass spec -> issue -> collect evidence -> fold into profile | getters/timing/target/sweep -> 1 pass runner + pass specs |
| **REVERSE** | minimal-pair \| corpus-diff \| live-acceptance-ladder, parameterized by the format element | ad-hoc per RE effort -> 1 harness |

Plus the **PROFILE** as the single source of every version-specific fact (already the direction), and a thin **PROVENANCE** tag on every result.

A new capability = one SCHEMA (what the element is) + one capability TAG + optionally one PASS spec. Zero new code paths.

## 3. Target skeleton (vs now)

```
flpkit/                         # pure, generic FORMAT CODEC (no FL, no orchestration)
  codec.py        locate/build/splice/verify - the ONE read+write engine, schema-driven
  schema.py       ElementSchema: how any .flp element is laid out (fields, detect rules)
  formats/        DATA only - notes.py playlist.py automation.py levels.py: schemas, no logic
  detect.py       signature detection (the pattern_base==20480 model), the ONLY place a size is inferred

fl_mcp/
  core/                         # generic pipelines, feature-free
    profile.py      the single fact source (loader; every constant comes from here)
    transport.py    Transport protocol + carriers {spool, udp, wire}; carrier chosen by capability
    router.py       Operation + target -> Lane, by capability evidence (generalized from note routing)
    discovery.py    Pass runner: spec -> evidence -> profile fold (the folded probe, generic)
    reverse.py      RE harness: minimal-pair / corpus-diff / live-acceptance
    provenance.py   CapabilityTag on every result (how+when verified)
  ops/                          # DATA + thin glue - what each capability IS
    notes.py playlist.py automation.py levels.py mixer.py params.py
      (each: the ElementSchema ref + the capability requirement + a 3-line lane binding)
  model/                        # the surface the agent drives
    sdk.py server.py scene.py schemas.py store.py
  fl/                           # the ONLY OS/FL-coupled code, quarantined
    paths.py process.py install.py    (pkill scope, OsxFL-from-plist, etc.)

native/
  abi/            fl_dispatch_ledger.h + fl_host_abi.h + generated/  (all facts from the profile)
  bridge/         the live executor: ONE command loop, carriers, discovery passes as data
```

Why this shape:
- **flpkit becomes a pure codec + format DATA.** Adding automation-write = adding `formats/automation.py` (a schema) - the codec already knows how to write it. This is the R4 B4-5 split taken to its conclusion: one engine, data descriptions.
- **`core/` holds the five generic pipelines**, each ~one readable module, feature-free. You read `router.py` once and understand ALL routing forever.
- **`ops/` is thin data+glue.** A capability is a schema + a capability tag + a lane binding. Mixer write becomes ~15 lines here, not a new subsystem.
- **`fl/` quarantines the OS-coupled code** (the brittle bits: pkill, process name, paths) in one place, so the rest is portable and testable without FL.
- **Nothing hardcodes a version fact** - `profile.py` is the sole source; `detect.py` is the sole place a size is inferred, always by signature with a warned fallback.

## 4. How every pending feature drops in (DATA, not code)

| Pending work | Becomes |
|---|---|
| automation-write | `flpkit/formats/automation.py` schema (writer path) + `fl_mcp/ops/automation.py` tag/binding |
| mixer control | `formats/mixer.py` (or a bridge op) + `ops/mixer.py` capability tag; routes through the SAME router |
| command mapper | a `discovery.py` Pass that aligns the scraped table to the expected map; no new subsystem |
| one-command discovery | `discovery.run_all()` sequencing existing Pass specs; the subprocess is a thin CLI |
| provenance tags | `core/provenance.py` consumed by scene/coverage; every op result carries one |
| E2E regression / fuzz | tests over the codec + router (generic), so they cover every format at once |
| bridge pre-warm, id 106 | one line in `router`/profile - not a feature |

## 5. Parallel plan (disjoint lanes, each CONSOLIDATES then enables)

Each lane is a REFACTOR that DELETES duplication and leaves a generic pipeline; features drop in after. Net-negative by construction. Frozen surfaces: the `ElementSchema`, `Transport`, `Operation`/`Lane`, `Pass`, `CapabilityTag` Protocols (defined in a shared-surfaces doc before spawn, like docs/27).

| Lane | Owns | Consolidates -> enables |
|---|---|---|
| **G1 codec** | flpkit: codec.py, schema.py, formats/, detect.py | fold 4 writers + readers into 1 schema-driven codec; enables automation-write + mixer as schemas |
| **G2 transport** | fl_mcp/core/transport.py + retire spool.py/wire.py/bridge_wire.py into carriers | one Transport + carriers; deletes the dead-carrier duplication (R4 B3-1) |
| **G3 router-ops** | fl_mcp/core/router.py (generalize), fl_mcp/ops/* | Operation routing over any capability; note/automation/mixer all one table |
| **G4 discovery** | fl_mcp/core/discovery.py + native discovery passes as data + the command mapper | generic Pass runner + auto-aligning mapper; one-command driver |
| **G5 quality+fl-quarantine** | fl_mcp/core/provenance.py, fl_mcp/fl/*, tests/e2e over the generic pipelines | provenance tags, OS-coupled code quarantined, generic E2E/fuzz that covers every format |

Ordering: G1 and G2 are independent foundations. G3 depends on G2's Transport (Lane bindings use it) - via the Protocol, so parallel with a stub. G4 depends on G2 (carriers) + G1 (schemas for target/sweep results). G5 is independent (tests + quarantine). An integration manager merges onto integrate/pipelines in order G1/G2 -> G3/G4 -> G5.

## 6. The bar (owner)
Generic, readable, no hacks: every lane must (a) end with FEWER total lines than it started, (b) add a capability only as data/schema/spec, (c) touch no version-specific constant except through the profile, (d) leave each pipeline module readable top-to-bottom as one idea. If a lane needs a special-case branch, that is the signal the pipeline is wrong - fix the pipeline, not the branch.
