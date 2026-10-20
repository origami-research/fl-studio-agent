# FL Studio MCP documentation

Status tags: **current** describes the shipped system, **research** records evidence or an active investigation, and **superseded** is retained only where it still explains an older decision.

## Start here

| Need | Read |
| --- | --- |
| Install and operate the engine | [SETUP.md](../SETUP.md), then [RUNBOOK_E2E.md](../RUNBOOK_E2E.md) |
| Check shipped capability and evidence | [24-coverage-map.md](24-coverage-map.md), [25-fl-capability-catalog.md](25-fl-capability-catalog.md) |
| Use the Python SDK | the `run` tool docstring, then [sdk-reference.md](sdk-reference.md) |
| Add or verify a capability | [33-capability-acquisition-model.md](33-capability-acquisition-model.md), [23-flp-reverse-engineering-playbook.md](23-flp-reverse-engineering-playbook.md) |
| Integrate the MCP engine | [`../contracts/fl-mcp.json`](../contracts/fl-mcp.json) |
| Run the local merge gate | [gate.md](gate.md) |
| Understand code-quality doctrine | `AGENTS.md`, [19-code-quality-proposal.md](19-code-quality-proposal.md) |

## Operational docs

- [SETUP.md](../SETUP.md) - **current** installation and first project.
- [RUNBOOK_E2E.md](../RUNBOOK_E2E.md) - **current** end-to-end verification.
- [gate.md](gate.md) - **current** local merge gate.
- [native/bridge/RUNBOOK_BRIDGE_UDP.md](../native/bridge/RUNBOOK_BRIDGE_UDP.md), [RUNBOOK_BRIDGE_V2.md](../native/bridge/RUNBOOK_BRIDGE_V2.md), [RUNBOOK_BRIDGE_V3.md](../native/bridge/RUNBOOK_BRIDGE_V3.md), [RUNBOOK_DISCOVERY.md](../native/bridge/RUNBOOK_DISCOVERY.md), and [WINDOWS.md](../native/bridge/WINDOWS.md) - **current** Bridge build, live verification, discovery, and Windows runbooks.

## Design and capability docs

- [00-overview.md](00-overview.md) - **superseded** original system overview.
- [01-fl-studio-surfaces.md](01-fl-studio-surfaces.md) - **research** FL integration surfaces.
- [02-architecture.md](02-architecture.md) - **superseded** original architecture.
- [03-protocols.md](03-protocols.md) - **superseded** original protocol design.
- [04-native-plugin.md](04-native-plugin.md) - **superseded** original plugin design.
- [05-controller-script.md](05-controller-script.md) - **superseded** controller-script design.
- [06-mcp-server.md](06-mcp-server.md) - **superseded** original server surface.
- [07-template-project.md](07-template-project.md) - **current** template-project contract.
- [08-roadmap.md](08-roadmap.md) - **superseded** roadmap snapshot.
- [09-references.md](09-references.md) - **research** external references.
- [10-v0-vertical-slice.md](10-v0-vertical-slice.md) - **superseded** v0 evidence.
- [11-interface-spec.md](11-interface-spec.md) - **current** interface and readback contract.
- [12-fl2026-native-surface.md](12-fl2026-native-surface.md) - **research** native-surface evidence.
- [14-spool-first-verdict.md](14-spool-first-verdict.md) - **superseded** spool decision.
- [15-plain-guide-and-fix-plan.md](15-plain-guide-and-fix-plan.md) - **superseded** implementation plan.
- [17-unified-architecture.md](17-unified-architecture.md) - **superseded** routing model.
- [18-capability-coverage.md](18-capability-coverage.md) - **superseded** coverage snapshot.
- [19-code-quality-proposal.md](19-code-quality-proposal.md) - **current** engineering quality plan.
- [21-v2-rewrite-spec.md](21-v2-rewrite-spec.md) - **current** shared-lane specification.
- [22-bridge-udp-design.md](22-bridge-udp-design.md) - **current** Bridge transport design.
- [23-flp-reverse-engineering-playbook.md](23-flp-reverse-engineering-playbook.md) - **current** file-lane evidence loop.
- [24-coverage-map.md](24-coverage-map.md) - **current** capability coverage map.
- [25-fl-capability-catalog.md](25-fl-capability-catalog.md) - **current** cross-surface evidence catalog.
- [26-master-plan.md](26-master-plan.md) - **current** dependency-ordered plan.
- [27-shared-surfaces.md](27-shared-surfaces.md) - **current** shared contracts.
- [29-generic-pipelines.md](29-generic-pipelines.md) - **current** reusable pipeline contract.
- [30-surfaces-and-readability.md](30-surfaces-and-readability.md) - **current** readability contract.
- [33-capability-acquisition-model.md](33-capability-acquisition-model.md) - **current** evidence ladder.
- [34-per-session-control.md](34-per-session-control.md) - **current** broker control lane.
- [oss-history-rewrite-plan.md](oss-history-rewrite-plan.md) - reviewable history-rewrite plan for a future public mirror (research).
- [37-oss-launch-decision.md](37-oss-launch-decision.md) - owner decision 2026-09-05: Bridge open, MIT, existing open-core boundary; what is still unauthorized
- [36-engine-contract.md](36-engine-contract.md) - generated MCP tool, error, and environment contract.
- [38-python-kernel.md](38-python-kernel.md) - **current** the `run` kernel, its OS boundary, and the Windows box contract.
- [26-master-plan.md](26-master-plan.md) - **superseded** pointer to the current canon.
- [sdk-reference.md](sdk-reference.md) - **current** SDK reference.
- [READING_GUIDE.md](READING_GUIDE.md) - **current** capability navigation.

## Research and evidence

- [research/current-summary.md](research/current-summary.md) - **current** research classification and synthesis.
- [research/automation-create-flp-re-2026-08-31.md](research/automation-create-flp-re-2026-08-31.md) - **research** automation gate.
- [research/insights-2026-08-31.md](research/insights-2026-08-31.md) - **research** capability findings.
- [research/mixer-routing-flp-re-2026-08-31.md](research/mixer-routing-flp-re-2026-08-31.md) - **research** mixer-routing gate.
- [research/oss-readiness-2026-09-01.md](research/oss-readiness-2026-09-01.md) - **research** open-source readiness.
- [research/plugin-add-minimal-pair-capture-checklist.md](research/plugin-add-minimal-pair-capture-checklist.md) - **research** plugin evidence checklist.
- [research/plugin-channel-state-flp-re-2026-08-31.md](research/plugin-channel-state-flp-re-2026-08-31.md) - **research** channel-state gate.
- [research/plugin-param-model-sytrus.md](research/plugin-param-model-sytrus.md) - **research** Sytrus parameter capture.
- [research/plugin-ui-action-trace-harness.md](research/plugin-ui-action-trace-harness.md) - **research** inert UI-action tracing.
- [research/r1-architecture.md](research/r1-architecture.md), [r2-ship-live.md](research/r2-ship-live.md), [r3-tier2-production.md](research/r3-tier2-production.md), and [r4-streamline-and-audit.md](research/r4-streamline-and-audit.md) - **research** implementation and discovery plans.
- [research/save-render-api-2026-09-01.md](research/save-render-api-2026-09-01.md) - **research** authoritative save/render verdict.
