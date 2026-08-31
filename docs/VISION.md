# the private product - vision v1 (owner-directed)

**the private product is LMArena for FL Studio.** A match opens TWO real FL Studio sessions side by side, each driven by a different (anonymous) model. Same brief. You watch them produce in the actual DAW, talk to either or both, click in and edit/drag-drop/import-export yourself, and VOTE which is better. It's a live, creative model-comparison arena - and it doubles as the best possible eval of "which model is the better producer."

The core primitive is **Cursor for FL Studio**: the AI edits your FL project - suggests changes, makes them, you steer and accept. That is exactly what the MCP + Bridge already is. The arena is a competitive wrapper around it.

## MVP (simplest real thing)
- **Format: two AIs, one human judge.** Two models, same brief, produce in two live FL sessions. Human watches/steers/votes. No human-produced entry needed yet.
- **Split screen**, LMArena-style: Session A | Session B, both streamed live. Chat box that can address A, B, or both. Vote button.
- **Full FL session exposure**: the human can click into either session, make changes, drag-and-drop clips, import/export `.flp`. It's a real FL, not a preview.
- **Competitive-first**: votes feed a model leaderboard (which model/adapter produces better, blind).

## The machine model (owner-specified)
- **One match = one machine running TWO FL sessions**, each on its own project, each with our Bridge + MCP, each driven by a different model/adapter.
- The models are just different API backends calling the same MCP tools - the machine hosts FL; we don't entangle model internals. "Adapter per session."
- This is the clean isolation unit: spin up a machine -> two FL sessions -> a match.

## The linchpin to prove first (honest)
Our Bridge tick currently only advances while FL is FRONTMOST (probe verdict: tick.frontmost_required=true). TWO FL sessions can't both be frontmost - so this must be solved before two-per-machine works. Two candidate solutions, likely combined:
1. **Virtual display per FL** (needed for streaming anyway): each FL renders into its own virtual display / GUI session, where it IS frontmost in its own context. The streaming infra and the tick fix are the SAME piece - a big synergy.
2. **App Nap opt-out** inside the plugin (NSProcessInfo beginActivityWithOptions, flagged in R4 B2-2): keep the tick running when the window isn't foreground.
Validate this FIRST - it gates the whole architecture. A one-machine-two-FL spike is the first build.

## How it maps to what we've built (we're well-positioned)
- The AI's hands = the open engine (fl_mcp + Bridge): compose, arrange, sound-design, automate, READ the project, render. Done.
- "Cursor for FL" = the MCP tools driving edits with readback. Done.
- Session isolation (two per machine) = separate spool dirs + MIDI ports + plugin instances. Mostly config; the tick-when-backgrounded fix is the real work.
- Streaming FL to the browser + input passthrough = the biggest NEW infra (WebRTC/remote-desktop over a virtual display).
- The arena wrapper (two sessions, brief, chat-A/B/both, vote, leaderboard) = the well-understood LMArena pattern.

## Build path
- **Spike 0 (the linchpin):** one machine, two FL sessions, both Bridges ticking simultaneously (virtual displays and/or App Nap opt-out), each driven by a different model on its own project. Prove it works. Nothing else matters until this does.
- **MVP:** stream both sessions to a split-screen web app; one brief -> both models produce; human chats/steers/clicks-in/votes; blind model leaderboard.
- **V1:** matchmaking, seasons, replays (the human's direction + each model's moves), import/export polish, drag-drop clip library.
- **Later (owner-noted, not now):** human vs AI, human-vs-human (30-min beat battles), AI judges, real-taste tests.

## Requirements
- The open engine (done-ish) + the two-FL-per-machine fix (Spike 0).
- FL streaming + input passthrough (virtual display -> browser, clicks/drags back).
- Match orchestration: spin a machine, launch two FL+model sessions, route the brief, collect the vote.
- Web app: split-screen stream, chat-to-A/B/both, vote, leaderboard.
- Backend: auth, match state, the model-adapter routing, storage of projects/replays, anti-abuse.
- A pool of licensed FL machines (two sessions each) that matches queue onto.

## Open question back to you
Streaming a full interactive FL GUI to the browser (with input passthrough) is the heaviest new piece. For the very first version, is watch+chat+vote (read-only stream, you steer via chat only) enough - with full click-in/drag-drop as a fast-follow? That would get an arena live much sooner.
