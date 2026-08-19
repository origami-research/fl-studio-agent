# 09 — References

## Primary sources (specs, headers, official docs)

- **FL Plugin SDK headers** — `fp_plugclass.h`, `fp_def.h`, `fp_cplug.h`, and bundled
  `SDK_documentation/` (obtained via the CMake template repo below). The authoritative
  source for `FHD_*` dispatcher constants, `TNoteParams`/`TNotesParams`, `NPF_*` and
  `FPF_*` flags. Key lines cited in these docs: `FHD_AddNotesToPR = 17`
  (`fp_plugclass.h:730`), `TNotesParams.ChanNum` comment, `FHD_WantIdle = 13`
  ("2 for always enabled"), `FPF_Type_Effect` (`fp_plugclass.h:532`).
- [FL Studio manual — MIDI Scripting (Python)](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/midi_scripting.htm)
- [FL Studio manual — Piano roll Scripting API](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/pianoroll_scripting_api.htm)
- [FL Studio manual — Exporting Audio & MIDI (command-line flags)](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/fformats_save_export.htm)
- [FL Studio API Stubs (IL-Group)](https://il-group.github.io/FL-Studio-API-Stubs/) —
  community-maintained, Image-Line-endorsed typed stubs & docs for the Python APIs;
  our reference for callback semantics (`OnIdle` ~20 ms cadence) and module inventories.
- [Image-Line — Developers page](https://www.image-line.com/developers) — SDK access.

## Prior art (code)

- [karl-andres/fl-studio-mcp](https://github.com/karl-andres/fl-studio-mcp) — MIT.
  MCP + controller script + keystroke-triggered piano-roll pyscript. Read for its tool
  taxonomy; its `utils/fl_trigger.py` (osascript keystroke injection) and singleton
  `mcp_request.json` mailbox are the anti-patterns our architecture removes.
- [MaddyGuthridge/Flapi](https://github.com/MiguelGuthridge/Flapi) — MIT, unmaintained
  (`Unmaintained.md`, main mid-refactor). Proof + pattern for SysEx→embedded-Python
  RPC over two virtual loopback ports.
- [demberto/PyFLP](https://github.com/demberto/PyFLP) — `.flp` parser/writer; our
  offline ground-truth reader.
- [Barsay/FL-Studio-SDK-CMake-Template](https://github.com/Barsay/FL-Studio-SDK-CMake-Template)
  — CMake scaffolding around the official SDK, with examples.
- [tonikasoft/fpsdk](https://github.com/tonikasoft/fpsdk) — Rust port of the SDK;
  useful second reading of dispatcher semantics (`AddToPianoRoll` message).

## Prior art (writeups)

- [BeatSage — "How We Got AI Into FL Studio"](https://beatsage.ai/blog/fl-studio-midi-breakthrough)
  — closed-source, but documents: SysEx-over-Windows-MIDI-Services control plane, the
  effect-registration + relocation trick for cross-channel `FHD_AddNotesToPR`
  ("nine words in a comment"), and the honest split between what SysEx covers and what
  needs the native plugin. Our independent confirmation target for Spike 1.
- [FL Scripting API functionality thread (Image-Line forums)](https://forum.image-line.com/viewtopic.php?t=309492)
  — years of evidence that no Python-API path writes persistent notes cross-channel.

## Context (product landscape)

- [Design Arena](https://www.designarena.ai/) — the playbook reference for the
  (deferred) arena product direction.
- [Artificial Analysis Music Arena](https://artificialanalysis.ai/music/leaderboard/instrumental)
  — existing prompt→song arena (Suno/Udio-class specialist models); why "producer
  bench" should benchmark general LLMs producing, not song generators.

## Facts pinned during research (with dates)

- fl-studio-mcp: MIT, ~3.1k LOC Python, last commit 2026-08-02, 109 stars (checked
  2026-08-19).
- Flapi: unmaintained notice 2025-05-02; only 2026 activity is CI housekeeping
  (checked 2026-08-19).
- FL embedded interpreter: Python 3.9 (API Stubs / community docs, checked 2026-08-19).
- FL CLI render flags `/R /E /F /O /M /Z` — manual, checked 2026-08-19.
