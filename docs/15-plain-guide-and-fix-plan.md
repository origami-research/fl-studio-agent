# FL Studio MCP: the whole thing, explained simply

Written 2026-08-27 on branch `spool-first-control`.
This is the plain-language guide to the entire system: what exists, how every piece works, why it exists, what we tried instead, and the plan to fix everything that is still rough.
If you read one doc, read this one.
For deep technical detail, each section links the numbered doc that owns it.

---

## 1. What are we building?

A way for an AI (like Claude) to be a music producer inside your real, running copy of FL Studio.
You talk to the AI, and it lays down drums, writes basslines, turns knobs, and mixes - live, on your screen, undoable with Ctrl+Z.

The AI talks to a small program we wrote called the **MCP server**.
"MCP" is just a standard plug format for giving AI models tools, like USB is for devices.
Any AI app that speaks MCP (Claude Desktop, Claude Code, others) gets our FL Studio tools for free.

## 2. Why is this hard? FL Studio has no front door

Most professional software has an official way for other programs to control it.
SolidWorks has one. Blender has one. Ableton sort of has one.
**FL Studio has none.** No socket, no API, no command pipe. You cannot write a normal program that says "hey FL, add a note."

What FL *does* have is five weird side doors, each with different powers:

| Door | What it is | What it can do | What it can't do |
|---|---|---|---|
| **MIDI controller scripts** | Python that runs inside FL, meant for hardware like DJ controllers | Transport, tempo, mixer, knobs, step sequencer, reading names/state | Can't write notes. Can't write files (we proved this). Can only be reached via MIDI |
| **Piano roll scripts** | Python that runs when a human clicks a menu inside the piano roll | Read AND write notes, full fidelity | Only runs on the currently-open piano roll, only when triggered |
| **Native plugins** | C++ effects/instruments loaded in the mixer | A special host call (`FHD_AddNotesToPR`) that writes notes to any channel, silently | Must be loaded by a human once (no API loads plugins); FL 2026 crashed on ours 11 times (solved, see section 6) |
| **Project files (.flp)** | The saved file on disk | Read everything (via the PyFLP library), including notes | Only when saved; not live |
| **Command line render** | `FL.exe /R` style offline export | Turn a project into audio | Nothing else |

Everything in this project is about combining those doors into one clean system, so the AI never has to know the doors exist.

One more door appeared in FL Studio 2026 and deserves its own paragraph:
FL now ships its own AI assistant ("Gopher") with an internal tool module (`MCPTools` + a native module called `ilmcp`) that CAN load plugins and run piano roll scripts programmatically.
We found it, decompiled it, and probed it live.
Verdict: the good parts (`ilmcp`) only exist inside FL's own AI session - our scripts cannot import it, and FL exposes no network endpoint to reach it.
We did salvage its ~40 public tools (they work through our transport), but the wall-breaking parts are locked. (Full story: doc 12.)

## 3. The pipeline today, end to end

```
You type: "give me a four-on-the-floor kick"
   |
   v
Claude (any MCP client)
   |  JSON tool call over stdio, e.g. write_notes([{key:"C5", start:0, ...}])
   v
MCP SERVER  (Python process on your machine - mcp-server/src/fl_mcp/)
   |  validates the notes, logs the action, then uses one of two paths:
   |
   |--- CONTROL PLANE (for everything except notes) ---------------------.
   |     server itself opens a virtual MIDI cable (control_midi.py)      |
   |     and sends a tiny JSON message as a SysEx packet                 |
   |         v                                                           |
   |     CONTROLLER SCRIPT (device_FLMCP.py, Python inside FL)           |
   |     receives it, calls the real FL function, replies in ~7ms        |
   |                                                                     |
   |--- NOTE PLANE (for notes, interim version) -------------------------|
         server writes a JSON file into a "spool" folder FL can read     |
             v                                                           |
         PIANO ROLL SCRIPT (MCP Write Notes.pyscript, inside FL)         |
         reads the file, stamps the notes into the open piano roll,      |
         writes a result file back                                       |
         (trigger today: one keyboard shortcut; endgame: the plugin      |
          does this silently with no trigger at all)                     |
                                                                         v
                                                             FL STUDIO, live
```

A concrete example that actually ran on this machine on 2026-08-26:
the server queued kick/hat/snare/clap patterns, focused each channel's piano roll over the control plane, triggered the note script, and FL played the beat.
Then separately: the server opened its own MIDI ports, FL connected at launch, and `setCurrentTempo` changed the live tempo in 7 milliseconds.

### The parts list (what each file is)

| Piece | File | Job |
|---|---|---|
| MCP server + tools | `mcp-server/src/fl_mcp/server.py` | The tools Claude sees (`write_notes`, `clear_notes`, `fl_status`) |
| Note validation | `schemas.py`, `notes.py` | Check every note is sane ("C5" -> 72, velocity 0..1) before anything touches FL |
| Control transport | `control_midi.py` | Server-hosted virtual MIDI ports + request/response matching. No extra processes |
| Note spool | `spool.py`, `flpaths.py` | Write/read the JSON files in FL's allowed folder |
| Installer | `install.py` | Copies both scripts into FL's folders and writes FL's own config file (`reg.xml`) so zero clicking is needed in FL |
| Controller script | `fl-scripts/controller/device_FLMCP.py` | Lives inside FL; executes control ops; also bridges to FL 2026's built-in `MCPTools` tools |
| Note script | `fl-scripts/piano-roll/MCP Write Notes.pyscript` | Lives inside FL; applies queued note edits to the open piano roll |
| Native plugin (endgame) | `native/` (on lane branches) | Will replace the note script + trigger with a silent in-FL worker |

There is also a second branch (`integrate/lanes-wave1`) holding the bigger tool set (~18 tools), a Python "notebook kernel" (the AI writes real Python like `fl.track("Bass").notes`), a records store, and an action log.
It is built and tested but not yet merged with this branch's live-verified transport. That merge is fix item 4 below.

## 4. Why each piece is the way it is (motivations + what we rejected)

### Why an MCP server at all?
Because it's the standard socket AI apps already speak.
We write the FL integration once; every MCP-speaking app gets it.
A custom "harness" (our own chat loop) can still be built later - it would call the same internals underneath.

### Why does control ride MIDI instead of files?
We didn't assume - we tested both, live.
Files would have been simpler (no ports, no cables), so we built the file version first and measured it.
Result: the controller-script interpreter is sandboxed - it can READ files but every attempt to WRITE one fails (`SystemError`), across four different strategies.
No response channel = no file transport. So MIDI stays, by proof rather than preference. (Doc 14.)

The friction people associate with MIDI (port setup, ghost devices, extra processes) turned out to come from HOW we hosted the port, not from MIDI itself.
Fixed by two changes: the server opens the port itself (no separate bridge program), and the installer writes FL's port assignment directly into FL's config file `reg.xml` with FL closed (no clicking in FL's settings).
Both are implemented and live-verified.

### Why do notes go through files and a piano roll script?
Because the note-capable interpreters are exactly the ones we can't reach directly.
The controller script (reachable, fast) can't write notes.
The piano roll script CAN write notes and CAN write files - but only runs when triggered.
So: server queues the notes in a file, the script applies them when it runs, and the trigger is the only ugly part.
Today's trigger is FL's own "run last script again" shortcut, automated (about 2 seconds per write, one brief window focus).
This is explicitly interim: the native plugin removes the trigger entirely.

### Why build a C++ plugin at all?
It's the only door that writes notes to ANY channel silently, with no trigger, no focus, no menus - the difference between a demo and a product.
The known open-source alternative (karl-andres/fl-studio-mcp) fakes keyboard shortcuts at the focused FL window for every single write; it works but steals your mouse/keyboard constantly.
We rejected that as the primary path on day one and kept it only as the interim trigger.

### Why is there a "shadow score" (server-side copy of the notes)?
Because FL cannot be asked "what notes are in this pattern?" - no door reads notes live.
So the server writes down everything it writes (its "records"), labels every answer with where it came from (memory vs freshly-parsed saved file), and can re-read ground truth by parsing the .flp.
An action log (`actions.jsonl`) additionally records every change, so a whole project can be reconstructed action by action.

### The graveyard (things we tried or seriously considered, and why they died)

| Idea | Why it died |
|---|---|
| Keystroke automation as the main note path (karl-andres style) | Steals focus on every write; breaks when you touch the mouse; kept only as the interim trigger |
| Depending on the Flapi library | Unmaintained, mid-refactor; we vendored the ideas (MIDI loopback + SysEx) instead |
| File-based control plane ("spool-first") | Proven impossible: controller interpreter can't write files (doc 14) |
| Using FL 2026's own `ilmcp` (plugin loading, script running) | Locked to FL's own AI session; not importable from our interpreters; no endpoint (doc 12) |
| A Python-ish text DSL for the model to write notes in | Redundant once the notebook kernel exists (the model just writes real Python) |
| A scratch memory / KV store for the model | The chat history already is the model's memory |
| Reading FL's memory directly for notes (game-trainer style) | Breaks every FL update; can corrupt projects; parked as last resort |
| Separate bridge process holding the MIDI ports | Caused ghost ports and lifecycle bugs; server now hosts the ports itself |

## 5. What's still rough, and the plan to fix all of it

In priority order. Items 1-5 make the current system solid; 6-8 make it shippable; 9-10 are polish.

**1. FL rejects some mutating ops with "Operation unsafe at current time" (small, next).**
FL sometimes refuses engine changes (like tempo) when asked from the MIDI callback mid-cycle.
Fix: in the controller script's dispatcher, catch exactly that error, park the op in a queue, execute it on the next `OnIdle` tick (which we proved fires ~every 20ms), and reply when it lands.
One function, plus a test. This also gives every future op safe-retry for free.

**2. Native plugin: crash SOLVED, autonomous self-poll RULED OUT (verdict 2026-08-27).**
The crash mystery is solved: FL 2026's internal plugin interface grew beyond the public headers (it calls vtable slots 26+), and the SDK base class + short vtable were the cause.
The base-free, 64-slot plugin now loads cleanly and stably, every time - proven live across many inserts as both an effect and a generator.
BUT the plan for it to *poll the note spool on its own* is dead, proven by live experiment:
- windowless effect => loads, but dormant (FL's idle callback is pumped by an editor-window run loop it doesn't have);
- windowless generator => loads (`have_host=1`), but receives ZERO callbacks while the transport is stopped (12s idle, not a single idle/tick/render call, the heartbeat file wasn't even rewritten);
- adding an editor window to get idle => reintroduces FL's window-embed crash on load.
So there is no callback source that fires while FL is idle+stopped without an editor, and the editor crashes.
The autonomous silent plugin is NOT achievable on FL 2026 macOS with the known SDK.
**Decision: ship the interim pyscript note path (it works).**
The plugin work is not wasted - the note *write* path (`FHD_AddNotesToPR`, base-free load, cross-channel) is proven and unit-tested; it pivots to a **driven model**: the server pokes the plugin on demand (via a plugin-parameter change through the controller script), so the plugin writes notes in its param callback - no idle, no editor needed. That becomes a fast-path upgrade over the interim later.
Also fix the quit-teardown crash (our dylib on the stack during FL shutdown - guard all callbacks against post-shutdown/null-host calls).

**3. Bake the one-time plugin insert into a template project (after 2, needs licensed FL).**
FL only loads plugins that are already in a project file, and saving a template needs a licensed FL (trial blocks save) - deferred until the license arrives.
Once we can save: insert the Bridge plugin once, save `flmcp-template.flp`, ship the file; every session opens it, zero clicks forever.
The template also carries pre-made empty patterns (pattern creation has no API on any door).
Until then the installer places scripts + `reg.xml` and marks the template step "pending license"; `fl_doctor` does not fail on a missing template.

**4. Merge the two branches (medium, high value).**
`spool-first-control` has the live-verified transport and installer; `integrate/lanes-wave1` has the ~18-tool surface, the notebook kernel, records, and the action log.
Merge them so the kernel's `fl.track("Bass")` calls ride `control_midi.py`, point the note tools at whichever note path is live (interim now, plugin later), reconcile with open PRs #12/#13, and land on main.
Result: one branch, one truth, full tool surface on a proven transport.

**5. Defer-safe port ordering + doctor (small).**
The virtual ports must exist before FL launches (FL only scans MIDI at startup).
The natural flow already satisfies this (start Claude session -> server opens ports -> open FL), but `fl_doctor` must detect the wrong order and say, in plain words, "restart FL once."
Optional later upgrade: a persistent IAC bus removes the rule entirely; deferred because enabling IAC programmatically is unreliable across macOS versions.

**6. Un-vendor the wire codec (small, before packaging).**
`control_midi.py` currently imports the frame codec from the controller script via a repo-relative path - fine in the repo, broken in a pip-installed package.
Fix: move the codec into a small shared module that both the server and the (standalone, stdlib-only) controller script embed, with a checksum test keeping them in sync.

**7. One-command distribution (small, after 4+6).**
Publish to PyPI so setup becomes exactly:
`claude mcp add fl-studio -- uvx fl-studio-mcp`.
First run self-provisions FL (the three file writes `install_all()` already does), tells you to open the template, done.
No clone, no venv, no manual anything.

**8. Fold everything into one installer + doctor (small).**
`install.sh` (integrate branch) still lists the crashing plugin build as step 1 and doesn't do the `reg.xml` write.
Rebuild it as a thin wrapper over `install_all()` + template placement + `fl_doctor`, with per-step PASS/WARN/FAIL.

**9. Wire the freshness signals (medium, after 4).**
The controller script already pushes FL's "something changed" events (`dirty_channel`, `refresh`, project load) and the server already buffers them (`drain_events`).
Connect them to the records store so name caches invalidate automatically and the model gets told when the human edited things. (Design: doc 11's freshness layers.)

**10. Windows + trace viewer (later).**
Windows: same architecture, different virtual-MIDI setup (loopMIDI bundle) and reg path - tracked, unstarted.
Trace viewer: a single HTML timeline of every tool call -> wire op -> FL response, built from the logs we already write; great for debugging and demos, not load-bearing.

### What "done" looks like

```
# once, ever:
claude mcp add fl-studio -- uvx fl-studio-mcp
# each session:
open the template project, talk to Claude
```
Notes appear silently via the plugin; knobs and transport respond in milliseconds via MIDI; everything the AI did is one Ctrl+Z away and one `actions.jsonl` away; no port dialogs, no clicking, no background processes you have to know about.

## 6. Doc map (where the deep detail lives)

| Doc | What it owns |
|---|---|
| 00-02 | Original vision, FL's doors, architecture (amended by 12/14) |
| 03 | Wire protocols (SysEx framing, spool format) |
| 04 | The native plugin design (superseded note-path; crash saga history) |
| 05-06 | Controller script, MCP server design |
| 07 | Template project strategy |
| 08 | Roadmap + open-questions ledger |
| 10 | The v0 vertical slice (note spool + pyscript) |
| 11 | Interface spec: types, kernel, references, freshness layers |
| 12 | FL 2026's built-in AI tooling: verified facts + what we can/can't use |
| 14 | Spool-first control verdict (failed) + the corrected transport now in place |
| 15 | This guide |
