# FL Studio 2026 native surface: verified facts, backend swap, readiness

Status: authoritative as of 2026-08-26, verified against FL Studio 2026 (26.1.5.5384, Apple Silicon) on this machine.
This doc supersedes the note-plane design in docs/04 (native C++ plugin) and the plugin-provisioning half of docs/07 (template project).
Method: strings on `FLEngine_x64.dylib`, full marshal-decode and disassembly of `MCPTools.pyc`, live process socket inspection, bundled piano-roll script reference and examples, and public press coverage.

## 1. Verified facts

1. FL Studio 2026 ships a native module, `ilmcp`, compiled into its engine, plus a Python wrapper `MCPTools.pyc` (~50 tools) at
   `/Applications/FL Studio 2026.app/Contents/Resources/FL/System/Tools/MCP/MCPTools.pyc`.
   This is what powers the built-in Gopher assistant (Alt+F1).
2. Every wall-breaking capability is a native `ilmcp` call:
   - `add_effect` / `remove_effect` → `ilmcp.addEffect(name, track, slot)` (empty name removes). Programmatic plugin loading by name.
   - `add_channel` / `remove_channels` → `ilmcp.addChannel(name)` / `removeChannel`. Programmatic instrument creation.
   - `run_piano_roll_script(source)` → `ui.showWindow('piano roll')` then `ilmcp.runPRScript(source)`. Runs arbitrary pyscript against the focused score.
   - `save_piano_roll_script` → `ilmcp.storePRScript`. Browser enumeration → `ilmcp.getBrowserNames`.
3. Everything else in MCPTools (transport, tempo, channel/mixer/playlist naming, volume, pan, colour, mute, solo, mixer routing matrix, step sequencer, plugin params by index or name, quantize, window show/hide, session snapshot) is a thin wrapper over the public FL scripting modules.
   We can replicate those without `ilmcp`.
4. FL 2026 bundles exactly one Python: CPython 3.12.1.
   There is no 3.9 anywhere in the bundle, which retires the predicted bytecode-version wall for importing `MCPTools` from our controller script (pending the live confirm in section 4).
5. Piano-roll scripting has full note fidelity: `flp.score.addNote/getNote/deleteNote/clearNotes`, `noteCount`, and per-note `number/time/length/velocity/pan/release/color/fcut/fres/pitchofs/slide/porta/muted/selected/group/repeats`.
   Real limits: `color` is 0-15; times are integer ticks (PPQ from `flp.score.PPQ`).
6. Note targeting is focus-dependent.
   A piano-roll script only sees the currently open score.
   To aim it: select the intended pattern, then `open_piano_roll_for_channel(N)`, then `run_piano_roll_script(source)`.
   There is no atomic "write notes to (channel, pattern)" call.
7. FL exposes NO external endpoint.
   The live FL process holds zero sockets; the internal `TMCPServer` feeds Gopher's webview via a JS bridge (`window.flHelper.onMCPTools`).
   Our own transport into FL's embedded Python (virtual MIDI + controller script) remains mandatory.
8. Two capabilities are absent even from FL's own tooling: playlist/arrangement clip placement, and automation clips.
   `arrangement`, `patterns`, `general`, `device` are imported by MCPTools but never called.
9. Readback return path: `runPRScript` returns a value natively, but the proven mechanism is the script writing a spool file inside FL's Settings tree (sandbox allows `open()` there; `os.remove` blocked; no `encoding=` kwarg).

## 2. What this changes (a backend swap, not a pivot)

The model-facing layer is untouched: MCP server, tool surface, notebook kernel, pydantic schemas, action log, controller script, virtual-MIDI transport, installer.
Three swaps at the bottom of the stack:

1. Note plane: the custom C++ Bridge plugin + file spool is replaced by control-plane calls to `ilmcp.runPRScript`.
   The plugin (never verified, crashed FL 2026 ten times) deletes.
2. The two API walls fall: plugin loading and instrument creation become `ilmcp.addEffect` / `ilmcp.addChannel`.
   The template project shrinks to at most a pattern bank (pattern creation still has no API).
3. Note readback becomes cheap via a dump script, so the shadow score demotes from source-of-truth to write-through cache and the freshness ladder (docs/11 layers 3-5) collapses toward "just ask FL".

Survivorship: KEEP the MCP server, schemas, kernel, action log, theory helpers, fake-FL tests, installer.
PROMOTE `control.py` and `device_FLMCP.py` (now the single path to every capability; Lane B2's `mcp.import_tools`/`mcp.call` bridge graduates to core).
DEMOTE records/shadow score and the template project.
RETIRE (only after the gating probe passes) all of `native/`, the spool note plane, the dispatcher probe, and their tests; archive docs/04.
Net tracked-tree delta: roughly -2,700 to -2,900 LOC.

## 3. Capability matrix (the product's final coverage)

| Capability | Mechanism | Status |
|---|---|---|
| Transport, tempo | public API wrappers | code done, live-unverified |
| Channel rack: name/select/vol/pan/colour/mute/solo/route/quantize | public API | code done, live-unverified |
| Step sequencer | `channels.setGridBit` | code done, live-unverified |
| Mixer: vol/pan/colour/mute/solo/full routing matrix | public API | code done, live-unverified |
| Plugin params (any VST/AU/native), by index or name | `plugins.*` | code done, live-unverified |
| Load/remove effect plugins | `ilmcp.addEffect` | gated on probe |
| Add/remove instruments (channels) | `ilmcp.addChannel` | gated on probe |
| Write notes to any channel's active pattern | selection + `ilmcp.runPRScript` | gated on probe |
| Read notes back (fresh, any open score) | dump script → spool file | gated on probe |
| Pattern selection / switching | public API | code path exists |
| Pattern CREATION | none anywhere | wall; template pattern bank |
| Browser / plugin-database enumeration | `ilmcp.getBrowserNames` | gated on probe |
| Playlist track metadata (name/colour) | `playlist.*` | trivial |
| Playlist/arrangement CLIP placement | none anywhere | HARD WALL |
| Automation clips (create/edit envelopes) | none anywhere | HARD WALL |
| Plugin internal state (e.g. wavetable editors) | none anywhere | HARD WALL (true of every approach) |
| Offline render | CLI render on saved copies | designed, unbuilt (M4) |
| Headless operation | FL requires GUI + license | out of scope by design |

## 4. The single gating probe - RUN 2026-08-26, VERDICT IN

Live result (full data: `fl-scripts/controller/probe/PROBE_REPORT.json`):

1. Transport: PASS. hello received, ping round trip 1-3 ms over virtual MIDI.
2. `import MCPTools`: FAILS raw (`No module named 'ilmcp'`); PASSES with a stub `ilmcp` injected. All 49 tools present.
3. Public-API tools through our transport: PASS. `get_session_context` returned the full live project JSON; `open_piano_roll_for_channel` opened the piano roll for a named channel.
4. `ilmcp`-bound tools: FAIL. `add_effect` and `run_piano_roll_script` raise - the native `ilmcp` module is registered only in FL's own MCP/Gopher session, not in the MIDI-controller interpreter.

Consequences: the ~40 public-API tools are ours today through the control plane.
Plugin loading, channel creation, and programmatic piano-roll script execution are NOT reachable from our transport; the note plane falls back to the C++ plugin or the keystroke-triggered pyscript, and the template project stays load-bearing for plugin/pattern provisioning.
Incidental fixes made during the run: the installed controller script had been hand-patched with `__file__` (undefined in FL's interpreter, killed the script at load - reverted to the repo copy); FL's MIDI assignment was completed by writing `reg.xml` directly (input+output port 1); stale CoreMIDI ghost endpoints (from a killed probe process) had bound FL to a dead port - cleared by restarting `MIDIServer`.

### The probe design (kept for re-runs on new FL versions)

From the controller-script interpreter, in order:

1. `import ilmcp` (and/or `sys.path` insert + `import MCPTools`) succeeds.
2. `get_session_context()` / `ilmcp.getBrowserNames(...)` returns real data (proves calls work outside Gopher's context).
3. `ilmcp.addEffect("Fruity Limiter", scratch track, slot)` actually loads the effect.
4. Select a scratch pattern, `open_piano_roll_for_channel(N)`, `run_piano_roll_script` that adds one note and dumps `noteCount` to a spool file; the note persists.

All pass → execute the swap and delete `native/`.
Import fails → `ilmcp` is scoped to Gopher only; keep the C++ plugin / manual pyscript as the note path and ship the ~40 public-API tools regardless.
Calls fail or no-op → same fallback; the note-write block would be tool-level, matching Gopher's advertised "no melodies" restriction.
Until the probe passes, NOTHING is deleted.
Harness: `fl-scripts/controller/probe/host_probe.py`, extended with the note-write step.
Prerequisite: the controller script assigned to the FLMCP virtual MIDI input in FL's MIDI settings (one-time, scriptable once Accessibility is granted).

## 4b. Interim note path - VERIFIED LIVE 2026-08-26

With `ilmcp` unreachable, the interim note plane was built and demonstrated end to end the same day:
a model-written beat (kick, hats, snare, clap on four channels of the 808 demo project) landed in FL's piano roll and played back, driven entirely from the pipeline.

Per-write sequence (packaged as `fl-scripts/controller/probe/apply_notes.py`, one command per write):
1. `server.write_notes(...)` queues the command on the spool (unchanged v0 path).
2. `open_piano_roll_for_channel(N)` over the control plane focuses the target channel (uses `flbridge.py`, the persistent host-side bridge).
3. FL's own "run last script again" shortcut (Cmd+Opt+Y) triggers the `MCP Write Notes` pyscript - after a one-time-per-FL-session menu prime (`--prime` does the menu walk automatically).
4. `result.json` is polled and matched by command id.

Cost per write: one brief FL focus flash and about 2 seconds.
Limitations: writes target the focused channel's active pattern; the shortcut needs one prime per FL session; focus contention with other apps is handled by retry.
This path retires the moment the native Bridge plugin loads cleanly (crash bisect running in a parallel lane) - nothing above the spool changes when it does.

Also live-verified along the way: FL auto-rebinds to re-created virtual MIDI endpoints (no reconfiguration needed after a bridge restart), and FL 2026 passes `(index, updateType)` to `OnDirtyChannel`/`OnDirtyMixerTrack` (script fixed to accept both shapes).

## 5. Readiness definition and sequence

Already done (156 tests green, against fake FL): MCP server (~18 tools), notebook kernel, records store, action log, schemas, controller script, macOS installer.

- M0: consolidate docs (this doc), extend `host_probe.py` with the note-write step.
- M1 (gating, needs one live FL session): run the probe, record the verdict.
- M2: rewire note ops onto the control plane; graduate `mcp.import_tools`/`mcp.call`; replicate the public-API tool set as controller ops.
- M3: readback via dump; shadow score demoted to cache; delete `native/` + spool note plane + their tests; archive docs/04.
- M4: installer covers controller script + MIDI port + template only; render plane; Windows parity.

"Fully ready" = M4 exit: a user runs `./install.sh`, opens the template, and a model can compose, sound-design, and mix in their live session with no manual FL configuration, on a machine we did not touch.
Realistic effort from today: one live probe session (about 30 minutes with a human present), then a few days of lane work for M2-M3, then installer polish.
The probe is the only step that can move the date materially.

## 6. Sources

- Local: `MCPTools.pyc` (fully decoded), `FLEngine_x64.dylib` (strings), bundled `Piano roll script reference.txt` and example scripts, `lsof` on the live FL process.
- [MusicRadar: control FL Studio with an agentic AI chatbot from inside the DAW](https://www.musicradar.com/music-tech/you-can-now-control-fl-studio-with-an-ai-chatbot-from-inside-the-daw)
- [RouteNote: FL Studio 2026 turns Gopher into your AI production assistant](https://routenote.com/blog/fl-studio-2026-gopher-ai-production-assistant/)
- [Image-Line forum: open feature request for an exposed MCP server](https://forum.image-line.com/viewtopic.php?t=340112)
