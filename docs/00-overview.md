# 00 - Overview

> **AMENDED (2026-08-26):** the "unlock" below (the C++ effect plugin + `FHD_AddNotesToPR`) is superseded by FL 2026's own native tooling (`ilmcp`).
> Plugin loading and instrument creation are no longer walls.
> See [12-fl2026-native-surface.md](12-fl2026-native-surface.md) for the current architecture and capability matrix.

## What we are building

An MCP (Model Context Protocol) server that lets an AI model act as a producer inside a
**live, visible** FL Studio session:

- write and edit notes in the piano roll of **any channel and any pattern**, with the
  changes appearing on screen the instant they happen, undoable with Ctrl+Z
- drive transport (play/stop/record, tempo, song position)
- mix (volume, pan, mute, solo, routing, track naming)
- do sound design by automating the parameters of already-loaded instruments and
  effects, including third-party VST/AU plugins
- program the step sequencer
- read back enough session state that the model knows what it's working with

The user experience: you open a template project, start the MCP server, and talk to a
model. You watch it lay down a drum pattern, write a bassline, pull the filter open on
your Serum patch - all in your real FL Studio, on your real project, in real time.

## Why this doesn't already exist

Every public attempt has died on the same wall:

1. FL Studio's **only** external interface is MIDI (plus its command line for offline
   rendering). There is no socket, no COM/AppleScript surface, no RPC.
2. FL's embedded **Python MIDI Controller Scripting API** is reachable over MIDI
   (SysEx smuggling) and covers transport/mixer/parameters well - but it **cannot write
   notes into the piano roll**. [VERIFIED - no note-writing function exists in the API]
3. The API that *can* write notes (`flpianoroll` piano-roll scripting) only runs when a
   **human clicks a menu item** inside FL. It cannot be invoked programmatically.
   [VERIFIED - Image-Line manual: piano roll scripts run once when invoked from the
   Scripts menu]

So existing projects either skip note writing, or fake the menu click. The
best-known open-source attempt ([karl-andres/fl-studio-mcp](https://github.com/karl-andres/fl-studio-mcp))
writes note requests to a JSON file, then uses `osascript`/`pynput` to **physically
send Cmd+Opt+Y to the focused FL Studio window** to trigger a piano-roll script. It
works, but it steals window focus, breaks if you touch the mouse, serializes everything
through one global mailbox file, and is unusable while you use your computer.

## The unlock

The FL Studio **native plugin SDK** (C++/Delphi) gives in-process plugins a
host-dispatcher channel with permissions the Python API doesn't have. One of them:

```c
// fp_plugclass.h:730
const int FHD_AddNotesToPR = 17;   // add a note to the piano roll, PNotesParams in Value
```

Its parameter struct contains the critical field:

```c
int ChanNum;   // -1 for plugin's channel, or selected channel if plugin is an effect
```

The narrative SDK documentation says a plugin can add notes "to the piano roll **for
the plugin's channel**" - which is why forums (and reportedly Image-Line themselves)
concluded cross-channel note writing was impossible. The header comment says otherwise:
**register the plugin as an effect and the target becomes the selected channel** - any
channel. [VERIFIED in header; cross-channel behavior REPORTED by BeatSage's engineering
writeup, which describes exactly this flag change; must be reproduced in Spike 1]

So the core of this project is a small native **effect plugin that produces no sound** -
its only job is to receive note-write commands from outside and call
`FHD_AddNotesToPR`. Everything else (transport, mixer, parameters) rides the
well-trodden Python-API-over-SysEx path.

## Prior art and what we take from each

| Project | What it is | What we take | What we reject |
|---|---|---|---|
| [karl-andres/fl-studio-mcp](https://github.com/karl-andres/fl-studio-mcp) (MIT) | MCP + controller script + keystroke-triggered pyscript | Tool taxonomy (transport/mixer/channels/plugins/piano-roll); proof of demand | The GUI keystroke hack; singleton JSON mailbox |
| [Flapi](https://github.com/MiguelGuthridge/Flapi) (MIT, unmaintained) | Remote Python REPL into FL over two virtual MIDI loopback ports | The transport mechanism (request/response MIDI ports, SysEx encoding). Vendor the ideas, not the package - `main` is mid-refactor and abandoned | Depending on it as a library |
| [BeatSage blog](https://beatsage.ai/blog/fl-studio-midi-breakthrough) (closed source) | Rust adapter + SysEx controller script + C++ plugin for note writing | Validation that the architecture works end-to-end; the effect-registration insight | Nothing to reuse - no code published |
| [PyFLP](https://github.com/demberto/PyFLP) | Offline `.flp` parser/writer, no FL needed | Read-back escape hatch: parse a saved project to ground-truth the score | Using it as the live write path (it's offline by definition) |
| [FL-Studio-SDK-CMake-Template](https://github.com/Barsay/FL-Studio-SDK-CMake-Template) | CMake wrapper around the official SDK + headers + examples | Build scaffolding for the native plugin | - |

## Scope

**In scope (v1):**
- Realtime note writing to arbitrary channels/patterns via the native plugin
- Full control plane over the Python API (transport, mixer, channels, patterns
  selection, plugin parameters, step sequencer)
- Session state read-back (names, counts, selection, params) + shadow score
- Template project + install tooling
- macOS and Windows

**Out of scope (v1), with reasons:**
- **Loading new VST/AU plugins.** No programmatic path exists - `FHD_ShowPlugSelector`
  and `FHD_ShowPicker` only open the picker UI for a human. [VERIFIED] Covered by the
  template project (07).
- **Creating new patterns.** No dispatcher or API call exists. [VERIFIED - no such
  constant in `fp_plugclass.h`; none in the Python API] Covered by the template
  project's pre-created pattern bank.
- **Headless / server-side operation ("the private product" backend).** FL requires a GUI
  session and a license per machine. The offline pipeline (PyFLP compose → `FL.exe /R`
  render) is documented in 01 as a future direction, but this product is a *live
  creative tool*, deliberately.
- **Audio recording, playlist arrangement editing beyond clip drops, Edison scripting.**
  Later.

## Names used throughout these docs

- **MCP server** - the external process the model talks to.
- **Bridge plugin** - our native C++ effect plugin (working name: *FL MCP Bridge*).
- **Controller script** - our Python MIDI scripting device script (working name:
  `device_FLMCP.py`).
- **Template project** - the shipped `.flp` with pre-provisioned instruments/patterns.
- **Spool** - the on-disk command/response directory the Bridge plugin polls.
