# FL Studio MCP — Design Documentation

An MCP server that gives an AI model full, **realtime** control of a live FL Studio
session — transport, mixer, plugin parameters, and (the part no public project has
shipped without GUI hacks) **writing notes into the piano roll of any channel, visibly,
while the user watches**.

This documentation is written before any implementation. It is the contract for what we
build and the record of why we build it this way.

## Reading order

| Doc | What it covers |
|---|---|
| [00-overview.md](00-overview.md) | Vision, prior art, why this doesn't exist yet, scope and non-goals |
| [01-fl-studio-surfaces.md](01-fl-studio-surfaces.md) | The four programmable surfaces of FL Studio, capability matrix, and the `FHD_AddNotesToPR` discovery |
| [02-architecture.md](02-architecture.md) | System components, the three planes, data flow, state ownership, threading |
| [03-protocols.md](03-protocols.md) | Wire protocols: SysEx control protocol + file-spool note protocol |
| [04-native-plugin.md](04-native-plugin.md) | The C++ "MCP Bridge" effect plugin — the load-bearing component |
| [05-controller-script.md](05-controller-script.md) | The Python MIDI controller script (control plane) |
| [06-mcp-server.md](06-mcp-server.md) | The MCP server: tool catalog, shadow score, error model |
| [07-template-project.md](07-template-project.md) | The template project strategy that covers the two API holes |
| [08-roadmap.md](08-roadmap.md) | Spikes (riskiest first), milestones, open-questions checklist |
| [09-references.md](09-references.md) | All sources |
| [10-v0-vertical-slice.md](10-v0-vertical-slice.md) | v0 vertical slice |
| [11-interface-spec.md](11-interface-spec.md) | Interface & representation spec: dialect ladder, pydantic types, reference system, action log, readback ladder, kernel |
| [12-fl2026-native-surface.md](12-fl2026-native-surface.md) | **Current authority.** FL 2026's native `ilmcp`/MCPTools surface (verified), the backend swap that retires the C++ plugin, capability matrix, gating probe, readiness |

> **2026-08-26 amendment:** FL Studio 2026 ships native tooling (`ilmcp` + `MCPTools.pyc`) that loads plugins, creates channels, and runs piano-roll scripts programmatically.
> [Doc 12](12-fl2026-native-surface.md) supersedes the note-plane design in doc 04 and the plugin-provisioning half of doc 07.
> Where any older doc conflicts with doc 12, doc 12 wins.

## Epistemic status legend

Claims in these docs are tagged where it matters:

- **[VERIFIED]** — read directly from SDK headers (`fp_plugclass.h`), official Image-Line
  manual, or the FL Studio API Stubs documentation.
- **[REPORTED]** — claimed by a credible third party (e.g. the BeatSage engineering
  writeup) but not yet reproduced by us.
- **[ASSUMED]** — our design inference; must be validated by a spike before we depend
  on it. Every ASSUMED item appears in the open-questions checklist in
  [08-roadmap.md](08-roadmap.md).

## One-paragraph summary of the whole design

FL Studio's only external interface is MIDI, and its embedded Python API cannot write
notes. We get around both limits with two in-process components: a **Python MIDI
controller script** that executes control commands (transport, mixer, plugin
parameters) received as SysEx over a virtual MIDI loopback, and a **native C++ effect
plugin** that polls a command spool directory on its idle callback and writes notes
into the live piano roll via the SDK host-dispatcher call `FHD_AddNotesToPR` — which
can target arbitrary channels *because the plugin registers as an effect, not a
generator*. An external **MCP server** exposes both paths as tools. The two remaining
API walls (can't load new plugins, can't create patterns) are covered by shipping a
**template project** with instruments and empty patterns pre-provisioned.
