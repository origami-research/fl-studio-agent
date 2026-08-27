# Maximal capability coverage: the full FL Studio surface

Goal: cover everything an AI producer can do in FL Studio. This maps every capability to the mechanism that reaches it, its status, and the build order.

## The key reframe

The "hard walls" (arrangement, automation, pattern creation, plugin loading) were walls for FL's **live API only**. The **project-write backend** rewrites the `.flp` file directly (parse events with PyFLP → edit/insert → save → reopen), so it can express **anything the file format holds** — which is nearly everything FL does. The frontier is not "FL has no API"; it is "how much of the `.flp` event grammar we correctly author." Only one thing is genuinely impossible: editing a plugin's *internal* saved state (an opaque vendor blob).

So there are three real mechanisms, and every capability routes to one:

- **LIVE** — the server-hosted MIDI control plane, in place, no reopen. Best UX; limited to what FL's scripting API exposes.
- **FILE** — project-write via `flp_writer`/PyFLP. Reaches the whole file format; costs an FL reopen. This is how the walls fall.
- **IMPOSSIBLE** — a plugin's internal state (Serum's wavetable, a preset's guts). True of every approach, including a human using a text editor.

The router (docs/17) already picks LIVE-first, FILE-fallback per capability; this doc is the capability catalog it routes over.

## Verified against real FL 2026 (2026-08-27)

Ran the actual server code (`control_midi` + the merged controller script) against a live FL 2026 session:

- Control plane connects (hello), ~7 ms/op round trips.
- Reads: `getCurrentTempo`, `channelCount` ✅.
- **Live tempo**: `setCurrentTempo` changed the project 130000 → 137000 in place, no reopen ✅.
- **Live undo**: `general.undo` ✅.
- Caveat found and fixed: right after a project load FL returns "Operation unsafe at current time" for mutating ops for up to a few seconds. The controller's OnIdle deferral was giving up at ~1 s; widened to ~4 s (`DEFER_MAX_TICKS`), and `set_tempo`'s live call waits accordingly. Mutations that are transiently unsafe now land once FL settles; the cascade still falls back to file if they never do.

The `set_channel_mix` live path and the driven Bridge plugin note write are the next live verifications (not yet run against real FL).

## Coverage matrix

Status: ✅ built · 🟡 partial · 🔵 reachable, unbuilt · ⛔ impossible.

### Notes / piano roll
| Capability | Mechanism | Status |
|---|---|---|
| Write notes to a channel/pattern | LIVE (pyscript) + FILE (flp_writer) | ✅ |
| Clear / replace notes | LIVE + FILE | ✅ |
| Read notes back (ground truth) | FILE (parse .flp) | 🟡 `sync_from_project` unbuilt |
| Per-note velocity/pan/color/slide/porta/fine-pitch | FILE (full note record) | 🟡 core fields ✅, extended fields 🔵 |
| Note selection state | LIVE (pyscript `score.selection`) | 🔵 |

### Channels / rack
| Capability | Mechanism | Status |
|---|---|---|
| List / rename / select channels | LIVE + FILE | ✅ |
| Volume / pan / mute / solo | LIVE + FILE | ✅ |
| **Create / delete channels** | FILE (insert channel events) or NATIVE `ilmcp.addChannel` | 🔵 |
| Route channel → mixer track | LIVE + FILE | ✅ |
| Step-sequencer grid | LIVE (`setGridBit`) | ✅ |
| Channel pitch / fine tune | FILE + LIVE (partial) | 🟡 |

### Patterns
| Capability | Mechanism | Status |
|---|---|---|
| Select / switch pattern | LIVE | ✅ |
| **Create new patterns** | FILE (insert PatternID events) | 🔵 (today: template pattern bank) |
| Name / color patterns | FILE | 🔵 |

### Playlist / arrangement (the big win)
| Capability | Mechanism | Status |
|---|---|---|
| Playlist track names / colors | LIVE | ✅ |
| **Place pattern clips on the timeline** | FILE (playlist arrangement events) | 🔵 |
| **Place / trim / move audio clips** | FILE | 🔵 |
| Arrange song sections (intro/verse/drop) | FILE (composition of clip placement) | 🔵 |

### Mixer
| Capability | Mechanism | Status |
|---|---|---|
| Volume / pan / mute / solo | LIVE + FILE | ✅ |
| Full send-routing matrix | LIVE + FILE | ✅ |
| Track naming / color | LIVE + FILE | ✅ |
| **Add / remove effects** | NATIVE `ilmcp.addEffect` (session-scoped) or FILE | 🔵 (FILE path is the general answer) |

### Plugins / sound design
| Capability | Mechanism | Status |
|---|---|---|
| Read / set parameters (any VST/AU/native), by index or name | LIVE (`plugins.*`) | ✅ |
| Load a plugin with a preset | FILE (splice plugin+state chunk) or NATIVE | 🔵 |
| Select plugin presets | FILE / NATIVE | 🔵 |
| Edit a plugin's *internal* state (wavetable, matrix) | — | ⛔ |

### Automation
| Capability | Mechanism | Status |
|---|---|---|
| Move a param live (no recording) | LIVE | ✅ |
| **Create / edit automation clips (envelopes)** | FILE (automation-clip events) | 🔵 |
| Link automation to a target param | FILE | 🔵 |

### Transport / project / render
| Capability | Mechanism | Status |
|---|---|---|
| Play / stop / tempo / song position | LIVE | ✅ |
| Undo | LIVE (`general.undo`) | ✅ |
| Time signature / project settings | FILE + LIVE (partial) | 🟡 |
| **Offline render to audio** (`FL.exe /R` / macOS equiv) | OS process on a saved copy | 🔵 |

## Build roadmap (by coverage value, each gated on real-FL verification)

Every FILE capability below is: author the `.flp` event structure → write it → **open in real FL and confirm it renders correctly** → lock it behind a round-trip test. The real-FL gate is non-negotiable (a wrong byte corrupts the project silently).

1. **`sync_from_project`** (finish note read-back) — small, high value; makes provenance honest. Uses the existing `.flp` reader.
2. **Pattern creation + naming** (FILE) — retires the template-pattern-bank workaround; prerequisite for arrangement.
3. **Playlist clip placement / arrangement** (FILE) — the single biggest coverage win; unlocks "produce a full song."
4. **Automation clips** (FILE) — the other headline wall; unlocks dynamics/movement.
5. **Channel + effect provisioning** (FILE) — create channels, load plugins, add effects without the template.
6. **Offline render** — turn a project into audio, closing the compose→listen loop.
7. **Extended note fields + note selection read** — fidelity + editing intelligence.

## Grounded feasibility of the FILE-write frontier (measured 2026-08-27)

Parsing real FL 2026 projects with PyFLP split the frontier by actual cost:

- **Tractable now (PyFLP round-trips it):** notes (done), and — with moderate event-authoring effort — pattern create/name, channel provisioning, project settings. These edit or insert well-supported event types and can be round-trip tested (write → re-parse → assert) before the real-FL confirm.
- **RE-heavy (PyFLP is fragile here):** playlist/arrangement clip placement and automation clips. PyFLP's own *reader* raised `KeyError` on a stock demo's arrangement (the clip→channel iid map), so authoring reliable *writes* is raw-byte reverse-engineering of the format, not a library call - a real program per feature, each with heavy real-FL verification (a wrong byte corrupts the project silently). This is the file-format analogue of the native-plugin saga: doable, but measured in sessions, not hours.
- **Impossible:** a plugin's internal state (below).

So "the walls fall via file-write" is true in principle; in practice notes + patterns + channels are near, while arrangement + automation are a serious RE investment. The roadmap order above still holds, but items 3-4 (arrangement, automation) are heavyweight and should be scheduled as their own RE projects with batched real-FL verification, not treated as quick wins.

## What stays impossible (be honest)

- A plugin's **internal** state: its wavetable, its preset's guts, its modulation matrix. We load plugins, pick presets, and set exposed parameters — we never reach inside the vendor's opaque blob. No approach can, short of that vendor's own API.

That single exclusion aside, the `.flp`-write frontier puts essentially the whole of FL's producer surface in reach. The work is authoring each event structure correctly and verifying it against real FL — which is now unblocked.
