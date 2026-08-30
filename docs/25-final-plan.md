# Final plan: what we can do, limitations, and the best path forward

State as of 2026-08-30, after the bridge call-contract fix (#65) and the probe live verdict (#74). This is the capstone; the living detail is docs/24-coverage-map.md.

## 1. What we can do TODAY (confirmed against real FL)

- **Compose** - write notes to any pattern. File lane (always, any target). Bridge lane: LIVE, no reload, to any PATTERN (PatNum honored, #74).
- **Arrange** - place pattern clips on the timeline (flpkit, FL-verified re-save round-trip).
- **Sound design** - list/read/set a generator's ~600 parameters, live.
- **Read the project** - scene analysis (key, per-channel digest), playlist/arrangement, automation curves - all decoded from the file.
- **Export** - headless WAV render (`OsxFL -R`, FL closed).
- **Observe (new)** - FL push-notifies the plugin, live, for transport (play/stop, position), selection/focus, context changes, and mixer routing. Cheap state getters (position, tempo, PPQ, selection) cost ~0ms vs an 8.7ms tick.

## 2. Hard limitations (now with probe verdicts, not guesses)

| Limitation | Why (evidence) | Fixable? |
|---|---|---|
| No LIVE note readback | No host getter exists (#74 getters + sweep) | No - reads stay file-based (Cmd+S + flpkit). Acceptable. |
| Channel targeting not live | ChanNum ignored; no channel object handed back by any dispatcher id (#74) | Channel writes stay on the file lane. Pattern targeting IS live. |
| No score-edit push | Not observed when editing (#74) | Poll-and-diff (affordable). Minor open item (draw did not register in-window). |
| Automation WRITE | opaque trailer + target-link bytes | RE via minimal-pair byte diff (docs/23 playbook). |
| Mixer routing / effect chains | opaque; no host call found | File RE or a write-side probe. Biggest production gap. |
| Add instruments / load presets | no known API; plugin state opaque | Template projects carry instruments; revisit with a write-side probe. |
| Audio clips / samples | not decoded | Later; channel-clip records already parse. |
| Bridge crashes ALL of FL | runs in-process | Bulkhead: out-of-process helper + circuit breaker (#54). Biggest durability risk. |
| CI dead / Windows parked | org billing / hardware | Owner lever / hardware. |
| FL dispatcher ids 94 (crash), 99 (hang) | #74 quarantine | Never call them. Encoded in probe_safety. |

## 3. Best path forward (prioritized by value x readiness)

**Now (unlock-ready, low new-RE):**
1. **Harden then promote the Bridge for note writes.** Build the bulkhead + circuit breaker FIRST (a bridge crash must auto-fall-back to the file lane, never take FL down), then turn the bridge on by default for pattern-targeted live writes. Keep the file lane for channel targeting + offline. Retires reload for the common case.
2. **Build the observer.** Push-subscribe to transport/selection/focus/context/routing (realtime, free from FL's own callbacks) + lazy poll-and-diff at checkpoints (before an action, after a tool, at save) for score/state. Surface a user-action event stream to the model over UDP (#58). Add agent-vs-human concurrency rules (never clobber a note the user is mid-edit on).
3. **Live reads via the found getters** (position, tempo, PPQ, selection, playing state) - replaces most of the MIDI controller wire's read side; note-CONTENT reads stay file-based. Then retire the wire's read plane (doctrine: delete the superseded mechanism).

**Next (needs bounded RE, high value - Tier 2 production):**
4. **Automation write** - minimal-pair byte diff of an FL-authored automation edit; the reader (flpkit 0.3) already maps the points.
5. **Mixer / effect chains, instruments/presets** - a WRITE-SIDE probe (the read-side probe is done; now hunt host calls that MUTATE routing/plugins) + file RE. Attack by care order.

**Continuous (enablers / future-proofing):**
6. **Fix CI** (owner) - restores the automated gate.
7. **RTTI scraper (#63)** - makes FL-format/ABI changes auto-detected; the probe runbook (#74) + this scraper are how we re-verify on any FUTURE FL version.
8. Retire pyflp after automation-write lands; delete native/probe once its verdicts are absorbed (doctrine).

## 4. Doing new things / checking future FL versions
The probe is reproducible (native/probe/PROBE_VERDICT_2026-08-30.md "How this was run") - re-run it on any new FL build to re-answer all four questions. The RTTI scraper will flag struct/ABI drift automatically. New capabilities follow the same loop: read-side probe -> write-side probe / file RE -> live-verify -> update docs/24.
