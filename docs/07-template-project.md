# 07 - The Agent Project

The engine works on one open FL project, the agent project: `Documents/Image-Line/FL Studio/Projects/FLMCP Agent.flp`, or the file `FLMCP_AGENT_PROJECT` names (`session/facade.py`, `make_agent_project.py`).
Whoever launches FL opens it; the engine never opens a project itself.
It is either the packaged session template or a user's own project prepared for the engine.

Updated 2026-10-22: this page described a planned 16-channel, 32-pattern template with a manifest that was never built.
What follows is what ships.

## What the engine needs from the project

- **The FL MCP Bridge on the Master insert, loaded when FL opens the project.**
  The engine cannot add a plugin to an open project: FL's `ilmcp.addEffect` is unavailable from the controller interpreter, and no Bridge op loads plugins.
  Without the Bridge, notes, render, `check` and the audio stream do not work.
- **One Bridge, after every other Master effect.**
  Render records the Bridge's own input, so effects after it on Master are not heard; two Bridges sharing one spool ledger misbehave.
  FL stores it as an effect tuple whose internal name (event 201) is `FL MCP Bridge`; FL loads whatever binary sits in `Plugins/Fruity/Effects/FL MCP Bridge/`.
- **A file the engine can read.** Reads overlay the saved project (`channel_names`, `get_scene`, note state), parsed with flpkit.
- **Patterns that already exist.** The engine cannot create patterns; note writes target an existing pattern (default 1).
- **Something on the playlist for a song-mode render** (an empty playlist refuses).
- **A file FL's trial can open, when the box runs the trial.** A trial FL cannot reopen a project a trial saved ([35-platform-seams.md](35-platform-seams.md) "Trial mode").

`check()` assumes 4/4 bars (`session/check.py`); a project in another meter gets wrong bar spans.
PPQ and tempo are read from the project, never assumed.

## The session template

`fl_mcp/assets/session-template.flp`, resolved with `fl-mcp-template` (never copied by path), SHA-256 `a080994a1b9d90b1e42b388e806d646f14f576f941ce2766ad43e7bf05daf2e4`.
`make_agent_project.create(force=True)` copies it to the agent project path; the Windows box installer and the arena's FL launch do this before every start.

- PPQ 96, 130 BPM, 4/4, no notes.
- Five channels: `808 Kick`, `808 Clap`, `808 HiHat`, `808 Snare` (samplers on FL factory samples) and `FLEX Bass` (FLEX).
- Pattern 1 on playlist track 1, its clip window the `0xFFFFFFFF` auto-length sentinel, so the clip grows with the pattern and a Full-song render covers everything written into Pattern 1.
  Measured on trial FL 2026 for Windows (26.1.6): with an explicit `0..1536` window the song stayed 4 bars (7.9 s at 130 BPM) after 16 bars were written; with the sentinel it grew to 16 bars (29.54 s).
- The FL MCP Bridge in Master slot 10 (index 9), the only Master effect.

It was written by tools, not saved by FL: FL's stock `Basic with limiter` template with its Emphasizer renamed to the Bridge, then the playlist clip and its window added.
Its version event still reads FL 25.1.99.

## A user's project as the agent project

A host may start a session from a user's own project, as the arena does when a user uploads one for a matchup.
It prepares the file before FL ever opens it, so the engine sees an ordinary agent project:

- flpkit splices FL's own captured Bridge tuple (`flpkit.add_effect(path, 0, "FL MCP Bridge", slot=...)`, captured from an FL 26.1.5 GUI insertion) into the Master insert's last free slot, or keeps a Bridge already on Master; the wrapper event's insert and slot are rewritten the way FL writes them.
- It turns off the project's "show info on open" panel (`flpkit.hide_info_on_open`), which otherwise holds FL's foreground at load.
- It refuses projects a trial FL saved, projects from a newer FL than the box's, FL 11 and older (no slot events to place the Bridge in), and a Master insert with every slot in use.
- The host copies the prepared file to the agent project path, with a zipped loop package's samples beside it (FL looks for a missing sample in the project's folder), and launches FL on it.

Verified live on trial FL 2026 for Windows (26.1.6, 2026-10-23): FL opened the spliced file and the Bridge's heartbeat came up fresh with one instance, for a stock demo song, an FL 12 project, a project with a missing VST, and a project with a missing sample (FL then shows "Problems loading the project", which the launcher acknowledges).
A trial-saved project opened empty behind a Buy dialog instead.
