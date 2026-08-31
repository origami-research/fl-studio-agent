# Reading guide: any capability in two files

This is the readability guarantee of docs/30 made navigable.
To understand any capability you read exactly two things: its feature file(s), which state the feature plainly, and the ONE pipeline it flows through, which you read once and understand forever.
If understanding a feature ever needs a third file or a special-case branch, the pipeline is wrong (docs/29 §6) - file that as a bug.

How and when each capability was verified is not prose: it is a `CapabilityTag` in the registry (`fl_mcp/core/provenance.py`), enforced by `tests/e2e/test_provenance.py`.

## The pipelines (read each ONCE)

| Pipeline | Module | The one shape |
|---|---|---|
| Codec | `flpkit/codec.py` | locate -> (decode \| encode) -> splice -> verify-by-readback, schema-driven |
| Transport | `fl_mcp/core/transport.py` | frame -> send -> await/drain over the best live carrier |
| Router | `fl_mcp/core/router.py` | Operation + evidence -> the best Lane, breaker-gated |
| Discovery | `fl_mcp/core/discovery.py` | Pass spec -> issue commands -> fold evidence into the profile |
| Provenance | `fl_mcp/core/provenance.py` | CapabilityTag registry: how + when + against which FL |

The OS/FL-coupled edge lives quarantined in `fl_mcp/fl/` (`paths.py`, `process.py`, `install.py`) so everything above stays portable and testable without FL.

## Capability -> its two files

| Capability | Feature file(s) | Pipeline |
|---|---|---|
| Notes read/write | `flpkit/formats/notes.py` + `fl_mcp/ops/notes.py` | Codec (+ Router for lane choice) |
| Playlist / arrangement | `flpkit/formats/playlist.py` + `fl_mcp/ops/playlist.py` | Codec (+ Router) |
| Automation read (write: planned) | `flpkit/formats/automation.py` + `fl_mcp/ops/automation.py` | Codec |
| Channel levels (vol/pan/pitch) | `flpkit/formats/levels.py` + `fl_mcp/ops/levels.py` | Codec |
| Tempo / transport | `fl_mcp/ops/tempo.py` | Router -> Transport |
| Plugin params | `fl_mcp/ops/params.py` | Router -> Transport |
| Live bridge writes | `fl_mcp/session/router.py` binding + `native/bridge/` executor | Router -> Transport |
| Discovery probes (`wp_*`) | one Pass spec per probe | Discovery |
| Scene analysis | `fl_mcp/scene.py` | Codec (read side) |
| Render (full mix) | `tests/e2e/test_matrix_render.py` (the `-R` contract) | fl/ (OS edge) |
| Where FL lives / is it running / install | `fl_mcp/fl/paths.py` / `process.py` / `install.py` | fl/ (OS edge) |

Until the generic-pipeline lanes (docs/29 §5) are fully merged, some feature files above are still folded into `flpkit/__init__.py` and `fl_mcp/session/`; the pipeline column is already the map to where each one lands.

## The quality net over the pipelines (this is why the two files stay honest)

| Net | Where | What it pins |
|---|---|---|
| Round-trip fuzz | `tests/e2e/test_roundtrip_fuzz.py` + `tools/fuzz/` | random valid elements -> patch -> re-read identity + byte fixpoint, every format at once |
| File-lane matrix | `tests/e2e/matrix.py` (`run_file_cycle`) | the surgical-write invariant over six real file shapes |
| Live matrix | `tests/e2e/test_matrix_live.py` | FL itself testifies to the same cycle |
| Compose->arrange->render | `tests/e2e/test_matrix_render.py` | headless render is real audio at least as long as the arrangement we wrote |
| Provenance | `tests/e2e/test_provenance.py` | tags consistent, never stale, planned capabilities ship no writer |
| Router rule | `tests/e2e/test_router_generic.py` | first-serving-lane, priority order, kind-agnostic (no per-feature branch) |
