# the private product - consolidated master plan (folds all prior plans)

## The product
**LMArena for FL Studio.** A match opens two real FL sessions side by side, each driven by a different anonymous model; same brief; you watch them produce, click into either, chat to A/B/both, and vote (Sound-vs-Sound). Competitive-first; MVP = two AIs + a human judge. Core primitive: **Cursor for FL** (the AI edits your project with readback; you steer/accept).

### References (note now, explore as we scale)
- **private-product.io** - existing: humans submit tracks, community "Sound-vs-Sound" A/B voting, 19 ranks, genre boards, a SEPARATE AI pool. Validates the A/B-vote + ranking mechanic. NAMING: "the private product" is taken - pick a distinct name before launch.
- **LMArena / design arena** - the blind A/B model-comparison + leaderboard pattern we copy.
- **Sample-flip competitions** - a mode to add later (given a sample, best flip wins).
Our differentiator vs all of these: the entries are produced LIVE by AIs in a real DAW you can click into and steer - not finished-track submissions.

## The engine (the AI producer) - hands AND ears
- **Hands (done):** compose (notes/any pattern), arrange (clips), sound-design (600+ live params), automate, READ the project, render to WAV.
- **EARS (NEW, critical - the missing half):** the AI must HEAR to produce. Two capabilities:
  1. **Listen to its own output**: render -> feed the audio to a multimodal (audio-in) model AND/OR extract features (tempo, key, loudness/LUFS, spectral balance, onsets, stereo) -> the AI judges and iterates (compose -> render -> LISTEN -> adjust). Closes the production loop.
  2. **Listen to reference samples**: a sample library the AI can pull from, analyze, choose, and flip. Needed for sample-flip and for taste-matching a brief.
  All of this is TOOLS on the engine, same as the note/param tools.
- **Tier-2 (in progress):** automation-create, mixer.
- **Discovery automation (done):** auto-map FL's commands per version (only unknowns need a live poke).

## The infra
- **Spike 0 (LINCHPIN):** two FL sessions on ONE machine, both Bridges ticking simultaneously. Blocker = the frontmost-tick dependency. Fixes (synergistic): a VIRTUAL DISPLAY per FL (each is "frontmost" in its own context - also what streaming needs) + App Nap opt-out in the plugin. Prove this first; nothing else matters until it works.
- **Streaming:** FL GUI -> browser over the virtual display (WebRTC/remote-desktop) + input passthrough (clicks/drags/drops back). MVP can be watch+chat+vote (read-only stream) first; click-in is a fast-follow.
- **Match orchestration:** spin a machine -> two FL+model sessions on two projects -> route the brief -> collect the vote. Model = an "adapter" (API backend) per session; the machine hosts FL, we don't entangle model internals.
- **FL machine pool** (two sessions each) that matches queue onto.
- **Service layer:** auth, storage (projects + replays), match state, model-adapter routing, anti-abuse. The file-based half (compose/analyze on `.flp`) scales in the cloud; the live half runs on the pool.

## Repo split (docs/32, in prep now)
- OPEN: the engine (fl_mcp + Bridge + controller + the audio-perception tools) + the KNOWLEDGE docs. Bridge is open.
- PRIVATE: this repo - web, app, cloud, agent orchestration, arena logic. Consumes the open engine as a dependency. Do NOT publish until launch.

## Implementation phases
- **P0 - Spike 0 + Ears MVP:** two-FL-per-machine ticking (virtual display/App Nap) + the render->listen->adjust loop. The two hardest de-risks, in parallel.
- **P1 - local match:** one machine, two FL, two models, one brief -> both produce -> render both -> vote in a local UI. No streaming yet. Proves the match works end to end.
- **P2 - streamed arena:** split-screen web app, watch+chat+vote, blind model leaderboard.
- **P3 - interactive + cloud:** click-in/drag-drop, matchmaking, seasons, the cloud pool, replays.
- **P4 - modes:** sample-flip, human-vs-AI, human-vs-human, AI judges.

## Parallel research streams (launched now)
- **R-EARS:** the audio-perception design - render->listen loop, multimodal-audio vs feature-extraction (or both), the sample library + analysis, the tool surfaces on the engine.
- **R-INFRA:** the linchpin - two-FL-per-machine (virtual displays, App Nap opt-out, isolation: spool/ports/plugin per instance) + FL streaming + input passthrough. Includes a testable Spike-0 the orchestrator runs live.
- **R-ARENA:** match orchestration + service layer + the LMArena wrapper (A/B vote, blind leaderboard, ranks) folding the private-product.io/design-arena references; the file-cloud-first path.

Each research stream produces a concrete plan + tool/surface specs; implementation follows in phases, each de-risked by its research.
