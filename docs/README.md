# FL Studio MCP documentation

This index maps the current documentation set. Read the smallest applicable
group; [24-coverage-map.md](24-coverage-map.md) and
[33-capability-acquisition-model.md](33-capability-acquisition-model.md) are
the authority for what is real and how new capability work is promoted.

## Start here

| Need | Read |
| --- | --- |
| Understand the product and current shape | [11-interface-spec.md](11-interface-spec.md), then [12-fl2026-native-surface.md](12-fl2026-native-surface.md) |
| Check what works, what is blocked, or what to build next | [24-coverage-map.md](24-coverage-map.md), [25-final-plan.md](25-final-plan.md) |
| Add or verify an FL capability | [33-capability-acquisition-model.md](33-capability-acquisition-model.md), [23-flp-reverse-engineering-playbook.md](23-flp-reverse-engineering-playbook.md), [25-fl-capability-catalog.md](25-fl-capability-catalog.md) |
| Work on the rewrite or shared lane contracts | [26-master-plan.md](26-master-plan.md), [27-shared-surfaces.md](27-shared-surfaces.md), [21-v2-rewrite-spec.md](21-v2-rewrite-spec.md) |
| Use the Python SDK | [sdk-reference.md](sdk-reference.md) |
| Integrate the MCP engine | [`../contracts/fl-mcp.json`](../contracts/fl-mcp.json) |
| Run the local merge gate | [gate.md](gate.md) |
| Understand code-quality doctrine | `AGENTS.md`, [19-code-quality-proposal.md](19-code-quality-proposal.md) |

## Current canon

Durable references and active implementation contracts:

- [11-interface-spec.md](11-interface-spec.md) — interface, representations, references, readback, and freshness.
- [12-fl2026-native-surface.md](12-fl2026-native-surface.md) — live FL 2026 native-surface facts and backend readiness.
- [19-code-quality-proposal.md](19-code-quality-proposal.md) — typed boundaries, verification, and enforcement plan.
- [21-v2-rewrite-spec.md](21-v2-rewrite-spec.md) — lane specification and recorded gate results.
- [22-bridge-udp-design.md](22-bridge-udp-design.md) — bridge transport, threading, failure containment, and idempotency.
- [23-flp-reverse-engineering-playbook.md](23-flp-reverse-engineering-playbook.md) — repeatable `.flp` discovery and acceptance loop.
- [24-coverage-map.md](24-coverage-map.md) — living capability/coverage map and maintenance rule.
- [25-final-plan.md](25-final-plan.md) — compatibility pointer to the current plan and evidence canon.
- [25-fl-capability-catalog.md](25-fl-capability-catalog.md) — evidence map across FL surfaces.
- [26-master-plan.md](26-master-plan.md) — dependency-ordered implementation plan.
- [27-shared-surfaces.md](27-shared-surfaces.md) — frozen contracts for parallel lanes.
- [29-generic-pipelines.md](29-generic-pipelines.md) — codec, transport, router, and discovery pipeline contracts.
- [30-surfaces-and-readability.md](30-surfaces-and-readability.md) — two-file readability contract.
- [33-capability-acquisition-model.md](33-capability-acquisition-model.md) — capability evidence ladder and acquisition model.
- [36-engine-contract.md](36-engine-contract.md) - generated MCP tool, error, and environment contract.
- [34-per-session-control.md](34-per-session-control.md) - machine-wide control broker and per-session addressing.
- [07-template-project.md](07-template-project.md) - template project conventions and limits.
- [sdk-reference.md](sdk-reference.md) — implemented SDK and write/render/analyze workflow.
- [READING_GUIDE.md](READING_GUIDE.md) — capability-to-feature/pipeline navigation and verification net.

## Background and historical context

These remain useful for their specific surface, but are not authoritative when
they conflict with the canon above.

| Files | Read when |
| --- | --- |
| [00-overview.md](00-overview.md), [01-fl-studio-surfaces.md](01-fl-studio-surfaces.md), [02-architecture.md](02-architecture.md) | onboarding or comparing the original system model |
| [03-protocols.md](03-protocols.md), [04-native-plugin.md](04-native-plugin.md), [05-controller-script.md](05-controller-script.md), [06-mcp-server.md](06-mcp-server.md) | tracing the original control/note-plane implementation |
| [08-roadmap.md](08-roadmap.md), [09-references.md](09-references.md), [10-v0-vertical-slice.md](10-v0-vertical-slice.md) | historical spikes, sources, or v0 provenance |
| [14-spool-first-verdict.md](14-spool-first-verdict.md), [15-plain-guide-and-fix-plan.md](15-plain-guide-and-fix-plan.md), [17-unified-architecture.md](17-unified-architecture.md), [18-capability-coverage.md](18-capability-coverage.md) | tracing superseded architecture decisions or older coverage snapshots |

## Research and evidence

These are current gate evidence or active detail plans. Do not delete a gate
artifact merely because its conclusion is negative; its evidence is part of
the acceptance contract.

- [research/current-summary.md](research/current-summary.md) — classification of every research artifact and durable synthesis.
- [research/automation-create-flp-re-2026-08-31.md](research/automation-create-flp-re-2026-08-31.md) — automation creation gate evidence.
- [research/mixer-routing-flp-re-2026-08-31.md](research/mixer-routing-flp-re-2026-08-31.md) — mixer routing gate evidence.
- [research/plugin-channel-state-flp-re-2026-08-31.md](research/plugin-channel-state-flp-re-2026-08-31.md) — plugin/channel placement gate evidence.
- [research/plugin-ui-action-trace-harness.md](research/plugin-ui-action-trace-harness.md) — inert UI-action discovery handoff.
- [research/r1-architecture.md](research/r1-architecture.md), [research/r2-ship-live.md](research/r2-ship-live.md), [research/r3-tier2-production.md](research/r3-tier2-production.md), [research/r4-streamline-and-audit.md](research/r4-streamline-and-audit.md) — detailed implementation and discovery plans referenced by the master plan.

## Supersession rule

When documents disagree, prefer live-verified evidence, then the current canon.
When a new capability changes coverage, update
[24-coverage-map.md](24-coverage-map.md) in the same change. Spent synthesis
notes belong in `research/current-summary.md`, not as parallel plans.
