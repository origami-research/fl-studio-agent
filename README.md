# private-product — FL Studio MCP

An MCP server that gives an AI model full, **realtime** control of a live FL Studio
session — including writing notes into the piano roll of any channel while you watch,
with no GUI automation hacks.

The unlock: a silent native **effect** plugin calling the FL SDK's `FHD_AddNotesToPR`
host-dispatcher command, which can target arbitrary channels precisely *because* the
plugin registers as an effect rather than a generator — a behavior documented only in
a header comment and widely believed impossible.

## Status

**Design phase.** Full architecture documentation is complete; implementation starts
with Spike 1 (validating the note-writing mechanism). See
[docs/08-roadmap.md](docs/08-roadmap.md).

## Documentation

Start at **[docs/README.md](docs/README.md)**. Contents:

| | |
|---|---|
| [00-overview](docs/00-overview.md) | Vision, prior art, why this doesn't exist yet |
| [01-fl-studio-surfaces](docs/01-fl-studio-surfaces.md) | FL Studio's four programmable surfaces + capability matrix |
| [02-architecture](docs/02-architecture.md) | Components, planes, state ownership, threading |
| [03-protocols](docs/03-protocols.md) | SysEx control protocol + file-spool note protocol |
| [04-native-plugin](docs/04-native-plugin.md) | The C++ Bridge plugin (the load-bearing component) |
| [05-controller-script](docs/05-controller-script.md) | The embedded-Python controller script |
| [06-mcp-server](docs/06-mcp-server.md) | Tool catalog, shadow score, error model |
| [07-template-project](docs/07-template-project.md) | Template project covering the two API walls |
| [08-roadmap](docs/08-roadmap.md) | Spikes, milestones, open-questions ledger |
| [09-references](docs/09-references.md) | Sources |

## Architecture in one diagram

```
 Model ◄─MCP─► MCP server ──SysEx / virtual MIDI──► controller script (Python, in FL)   [control]
                    │────── JSON spool files ─────► Bridge plugin (C++ effect, in FL)    [notes]
                    └────── FL.exe /R ────────────► offline render on saved copies       [render]
```
