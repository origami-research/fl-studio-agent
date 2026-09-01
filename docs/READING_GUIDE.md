# Reading guide: any capability in two files

This is the navigable form of the readability contract in [docs/30](30-surfaces-and-readability.md).

To understand any capability, read its feature file and the one pipeline it flows through. If understanding a feature needs a third file or a special-case branch, file that as a pipeline bug.

How and when each capability was verified is not prose: it is a `CapabilityTag` in `fl_mcp/core/provenance.py`, enforced by `tests/e2e/test_provenance.py`.

## The pipelines

| Pipeline | Module | Shape |
|---|---|---|
| Codec | `flpkit/codec.py` | locate -> decode/encode -> splice -> verify-by-readback |
| Transport | `fl_mcp/core/transport.py` | frame -> send -> await/drain over the best live carrier |
| Router | `fl_mcp/core/router.py` | operation + evidence -> first serving lane |
| Discovery | `fl_mcp/core/discovery.py` | pass spec -> commands -> evidence -> profile update |
| Provenance | `fl_mcp/core/provenance.py` | machine-readable verification tags |

The OS/FL-coupled edge lives in `fl_mcp/fl/` (`paths.py`, `process.py`, `install.py`) so the generic pipelines stay portable.

## Capability map

| Capability | Feature file(s) | Pipeline |
|---|---|---|
| Notes read/write | `flpkit/formats/notes.py` + `fl_mcp/ops/notes.py` | Codec + Router |
| Playlist / arrangement | `flpkit/formats/playlist.py` + `fl_mcp/ops/playlist.py` | Codec + Router |
| Automation read/write existing clip | `flpkit/formats/automation.py` + `fl_mcp/ops/automation.py` | Codec + Router |
| Channel levels | `flpkit/formats/levels.py` + `fl_mcp/ops/levels.py` | Codec + Router |
| Tempo | `flpkit/formats/tempo.py` + `fl_mcp/session/controls.py` | Codec / control wire |
| Plugin parameters | `fl_mcp/ops/params.py` | Router + Transport |
| Live bridge writes | `fl_mcp/session/notes.py` + `native/bridge/` executor | Router + Transport |
| Discovery probes (`wp_*`) | one pass spec each in `fl_mcp/passes/` | Discovery |
| Scene analysis | `fl_mcp/scene.py` | Codec read side |
| Render | `tests/e2e/test_matrix_render.py` | `fl_mcp/fl/` OS edge |
| FL path/process/install | `fl_mcp/fl/paths.py`, `process.py`, `install.py` | `fl_mcp/fl/` OS edge |

Every row above is live in the tree as written. Thin public wrappers such as `write_notes` are compatibility API over the canonical pipeline, not second implementations.

## Quality net

| Net | Where | What it pins |
|---|---|---|
| Round-trip fuzz | `tests/e2e/test_roundtrip_fuzz.py` + `tools/fuzz/` | random valid elements -> patch -> re-read identity + byte fixpoint |
| File-lane matrix | `tests/e2e/matrix.py` | surgical-write invariants over real file shapes |
| Live matrix | `tests/e2e/test_matrix_live.py` | FL itself testifies to the same cycle |
| Compose -> arrange -> render | `tests/e2e/test_matrix_render.py` | rendered WAV validates the top-level workflow |
| Provenance | `tests/e2e/test_provenance.py` | capability tags are current and honest |
| Router rule | `tests/e2e/test_router_generic.py` | first-serving-lane, priority order, kind-agnostic |
