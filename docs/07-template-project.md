# 07 - The Agent Project

The engine works on one open FL project, the agent project: `Documents/Image-Line/FL Studio/Projects/FLMCP Agent.flp`, or the file `FLMCP_AGENT_PROJECT` names (`session/facade.py`, `make_agent_project.py`).
Whoever launches FL opens it; the engine never opens a project itself.
It is either the packaged session template or a user's own project prepared for the engine.

Updated 2026-10-22: this page described a planned 16-channel, 32-pattern template with a manifest that was never built.
What follows is what ships.

## What the engine needs from the project

- **The FL MCP Bridge on the Master insert, loaded when FL opens the project.**
  The engine cannot add a plugin to an open project: FL's `ilmcp.addEffect` is unavailable from the controller interpreter, and no Bridge op loads plugins.
  The engine requires it: without a running Bridge, note writes and render are refused, `fl-mcp-doctor` reports `not_ready`, and the audio stream does not start (`check` still plays, but whatever song FL has open).
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

`fl_mcp/assets/session-template.flp`, resolved with `fl-mcp-template` (never copied by path); its SHA-256 is pinned in `mcp-server/tests/test_template.py`.
`make_agent_project.create(force=True)` copies it to the agent project path; the Windows box installer and the arena's FL launch do this before every start.

Updated 2026-11-02: the template was five channels (four 808 samplers and a FLEX bass, all on the master), and models had nothing to play chords, pads or leads on.
It is now a palette of FL's stock instruments on a mixed bus layout, and it says in its own notes what each channel is for.

### What it contains

- PPQ 48 (the stock template's), 120 BPM, 4/4, no notes; Pattern 1 on playlist track 1 with the auto-length clip window (below).
- 23 channels, each on its own named mixer track:
  three FPC kits (808, house, acoustic jazz) whose pad notes the notes list, a pitched 808 cowbell sampler, an 808 (FLEX) into Fruity Fast Dist and Fruity Soft Clipper, electric bass, synth bass, piano, Rhodes, strings, pad, choir, brass, woodwinds, sax, guitar, lead and pluck, and five spare channels for live loading: two synths (3x Osc, Sytrus) and three samplers.
- The mixer of FL's "Advanced/Electronic/Trap" template: instrument tracks into drum, instrument, background, vocal and sub buses, a kick-driven sidechain (Fruity Peak Controller) feeding the sidechained and sub buses, a mid/side pre-master, and Maximus on the master.
  Measured 2026-11-09 on trial FL 2026 for Windows: a kick into the SideChain track left a pad on the Sidechained Bus unducked, so the palette guide's "ducked by the kick" does not hold on this build ([note](research/sidechain-samples-live-2026-11-09.md)). The builder no longer says it (2026-11-10); the guide is part of the built file, so the packaged template drops it at its next build. Pumping is drawn live with `fl.automate` ([note](research/live-pumping-2026-11-10.md)).
  Added: a Drum Bus with Parametric EQ 2 and a Fruity Limiter compressing, a Reverb Send (Fruity Reeverb 2) fed by the melodic tracks, a Delay Send (Fruity Delay Bank) fed by Lead and Pluck, and the FL MCP Bridge in Master slot 10, the last effect.
- The project's comments (FL's File > Project info) hold the palette guide: one line `<channel name>: <what it is for>` per channel, then how the song and the mixer work.
  `get_scene` returns each channel's line as its `about` and the rest as `project_notes`, and `fl.channels` carries `about`, for this template or any project written the same way (`flp.project_guide`).

### How it is built

`scripts/build_session_template.py`, run on a machine with FL Studio 2026 installed, writes it from FL's own stock content; the script's palette table is the one list of facts about each channel.
Each channel is a channel FL saved in a stock template (default channel settings) carrying an instrument FL saved elsewhere: a stock preset (FPC kits, the DirectWave instrument packs, Sytrus) or a stock template's or demo song's channel (FLEX patches, DirectWave pianos and band instruments).
Effects are FL-saved mixer-slot tuples from stock templates and demo projects, spliced with flpkit's `EffectFormat`; insert names, routes and fader levels are single-event edits; channel volumes go through flpkit.
The build reads every edit back, flpkit re-reads the result, and building twice gives the same bytes.

Legal to ship: no audio is copied; every sample stays in FL's factory folders and is referenced by `%FLStudioFactoryData%` or `%FLStudioData%` paths that FL resolves in its own install, and the instruments and effects are FL's own plugin states, as in any shared project (a test checks for paths and the absence of WAV or Ogg data).

### FL trial and editions

A trial FL cannot reopen a project a trial saved; the template is written by tools from files a licensed FL saved, so a trial opens it (verified on trial FL 2026 for Windows, 26.1.6, 2026-11-02: all 23 channels loaded and sounded under `check`, and a render recorded every one).
The palette uses plugins of FL's full editions (DirectWave, Sytrus, FPC, FLEX, 3x Osc, Maximus); an edition without one runs it in demo mode.

### The playlist clip

Pattern 1 sits on playlist track 1 with its clip window the `0xFFFFFFFF` auto-length sentinel, so the clip grows with the pattern and a Full-song render covers everything written into Pattern 1.
Measured on trial FL 2026 for Windows (26.1.6): with an explicit `0..1536` window the song stayed 4 bars (7.9 s at 130 BPM) after 16 bars were written; with the sentinel it grew to 16 bars (29.54 s).

## A user's project as the agent project

A host may start a session from a user's own project, as the arena does when a user uploads one for a matchup.
It prepares the file before FL ever opens it, so the engine sees an ordinary agent project:

- flpkit splices FL's own captured Bridge tuple (`flpkit.add_effect(path, 0, "FL MCP Bridge", slot=...)`, captured from an FL 26.1.5 GUI insertion) into the Master insert's last free slot, or keeps a Bridge already on Master; the wrapper event's insert and slot are rewritten the way FL writes them.
- It turns off the project's "show info on open" panel (`flpkit.hide_info_on_open`), which otherwise holds FL's foreground at load.
- It refuses projects a trial FL saved, projects from a newer FL than the box's, FL 11 and older (no slot events to place the Bridge in), and a Master insert with every slot in use.
- The host copies the prepared file to the agent project path, with a zipped loop package's samples beside it (FL looks for a missing sample in the project's folder), and launches FL on it.

Verified live on trial FL 2026 for Windows (26.1.6, 2026-10-23): FL opened the spliced file and the Bridge's heartbeat came up fresh with one instance, for a stock demo song, an FL 12 project, a project with a missing VST, and a project with a missing sample (FL then shows "Problems loading the project", which the launcher acknowledges).
A trial-saved project opened empty behind a Buy dialog instead.
