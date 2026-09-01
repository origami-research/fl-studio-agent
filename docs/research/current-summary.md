# Research audit and current summary

Status: current as of 2026-09-01. This file classifies every
`docs/research/*.md` artifact and preserves the durable conclusions from the two
spent 2026-09-01 synthesis notes it replaces.

## References checked

- Open GitHub gates: #54, #63, #64, #93.
- Current docs references: `docs/23-flp-reverse-engineering-playbook.md`,
  `docs/24-coverage-map.md`, `docs/25-final-plan.md`,
  `docs/25-fl-capability-catalog.md`,
  `docs/26-master-plan.md`, `docs/27-shared-surfaces.md`,
  `docs/28-forward-plan.md`, `docs/29-generic-pipelines.md`,
  `docs/33-capability-acquisition-model.md`, and `docs/sdk-reference.md`.
- Current code/test references: `mcp-server/src/fl_mcp/profile.py`,
  `mcp-server/src/fl_mcp/gen_layout.py`,
  `mcp-server/src/fl_mcp/scrape_rtti.py`,
  `mcp-server/src/fl_mcp/discover.py`,
  `mcp-server/src/fl_mcp/passes/plugins.py`,
  `mcp-server/tests/e2e/test_automation_create_minimal_pair.py`,
  `mcp-server/tests/e2e/test_mixer_minimal_pair.py`,
  `mcp-server/tests/e2e/test_plugin_state_minimal_pair.py`, and
  `mcp-server/tests/test_discovery.py`.

## Classification

| File | Classification | Evidence-backed action |
| --- | --- | --- |
| `automation-create-flp-re-2026-08-31.md` | Active gate evidence | Keep. `docs/24` cites it for the automation new-clip/target-link `NONE` row, and the strict E2E gate requires a real FL-authored one-action pair before a creator exists. |
| `mixer-routing-flp-re-2026-08-31.md` | Active gate evidence | Keep. `docs/24` cites it for mixer routing `NONE`; the xfailed E9 gate owns the missing baseline/changed FLP pair and rejects a public writer seam until evidence lands. |
| `plugin-channel-state-flp-re-2026-08-31.md` | Active gate evidence | Keep. `docs/24` cites it for effect chains, instruments, and presets; it is the placement proof for opaque `201/212/213` tuples, not a mutation contract. |
| `plugin-ui-action-trace-harness.md` | Current inert discovery handoff | Keep. It documents that `fl_mcp.passes.action_trace(profile)` observes only and issues no dispatcher command; tests pin that the trace command list is empty and fatal ids remain forbidden. |
| `r1-architecture.md` | Current detail plan | Keep. `docs/26` and `docs/27` refer to the R1..R4 detail set; R1 carries the session split, flag-removal, router, breaker, and probe-deletion sequencing. |
| `r2-ship-live.md` | Current detail plan | Keep. `docs/26` and `docs/27` depend on the R1..R4 detail set; R2 carries the bridge promotion, observer, live getter, and wire-retirement contracts. |
| `r3-tier2-production.md` | Current detail plan | Keep. `docs/26` points Tier-2 production work at R3; it is the detailed experiment matrix for automation, mixer, effect chains, instruments, presets, and samples. |
| `r4-streamline-and-audit.md` | Active profile/discovery reference | Keep. It is cited by numbered docs and by `profile.py`, `gen_layout.py`, and `scrape_rtti.py` for the profile schema and RTTI/discovery design. |
| `insights-2026-09-01.md` | Spent synthesis | Delete after consolidation. Its durable conclusions are below: the project is a two-engine capability acquisition loop, plugin candidates are not shipped capability, receipts must remain readback-based, and shared-session isolation is the next Arena-facing invariant. |
| `improvement-plan-2026-09-01.md` | Spent synthesis | Delete after consolidation. Its durable conclusions are below: restore truthful verification gates, enforce repo/branch identity before mutation, type the evidence ladder, finish boundary cleanup, and execute automation/mixer as proof-first lanes. |

## Durable synthesis

The engine is now best understood as two reusable acquisition paths, not as
one-off FL feature work. `.flp` data goes through flpkit-style codec work:
FL-authored minimal pairs, byte diffs, save/reparse verification, and live
acceptance where required. Live host behavior goes through discovery/profile
work: static RTTI and dispatcher inspection, bounded probes, fatal-id quarantine,
and promotion only after disposable-project readback.

Static plugin discovery is useful candidate generation, not a callable plugin
surface. Effects, instruments, presets, and render-related host calls stay behind
the same evidence ladder as every other capability: static, decoded, live
verified, or rejected must remain distinct states. A candidate must not route a
write.

The highest-value remaining process fixes are executable gates rather than more
planning prose: a reproducible local/CI verification command, repository and base
identity checks before mutation, typed evidence/provenance on capability claims,
and boundary results that distinguish applied, confirmed, and history-recorded
states.

For product/Arena use, two FL sessions are plausible at the process/tick layer,
but not complete until spool roots and UDP ports are namespaced per session and a
two-session write/readback acceptance test exists. Render and ears loops should
stay bounded and artifact-based; do not turn blocking UI export into an automatic
feedback tick.
