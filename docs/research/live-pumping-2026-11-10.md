# Pumping drawn live with fl.automate, and the run kernel's start (2026-11-10)

**Scope.** The kick ducking a pad or bass, drawn live with `fl.automate` on each ducked part's mixer track volume instead of bounced through numpy and a sampler ([earlier note](sidechain-samples-live-2026-11-09.md)).
Also why the run kernel missed its start limit on a freshly reset box (#324).
Probe scripts, logs and WAVs are kept outside the repository (`~/.cache/release-019` on the operator's Mac).

## The probe

Kick (Drums House, MIDI 36) on every beat for 16 bars, a sustained Pad chord and a Synth Bass note per beat.
One `fl.automate` per part on its mixer track's volume (Pad 17, Synth Bass 12): per beat, down to 0.25 on the kick, back to the track's volume (0.8 and 0.744) over 0.35 beats, held until the next kick; 191 points over 63 beats.
Then stems of kick, pad and bass, and the pad's level compared with the same stems rendered without the automation (5 ms RMS windows, per beat from the kick stem's onsets).

## Results

| Run | What happened |
| --- | --- |
| Playback check, as built in #321 | Rejected every take: "up to 0.55 away from the values asked for". FL plays a recorded value until the next idle tick's, so where the line falls or rises steeply playback trails it by up to one recording step. The check now allows that step (the controller reports the widest step it recorded); with it both takes read back `max_error` 0.0. |
| A take from bar 1 to the song's end | The playback ran past its window ("FL played past the job's window"): FL went back to the song's start, which from bar 1 is not before the start, so the controller never saw the take end. Any step backwards now ends it. |
| Depth, first take (dips on the kick) | Pad: 16.9 dB a beat (median; 55 of 64 beats at least 10 dB). But the dip's bottom landed 125 ms after the kick (median; at most 195 ms). |
| Depth, dips 0.2 beats (0.1 s) early | Pad: 14.1 dB a beat (median; 53 of 64 at least 10 dB; per bar 8.0 to 18.8, no drift from bar 1 to 16), deepest 35 ms after the kick (median). The beat-folded gain: -8.5 dB at the kick, -4 dB at 120 ms, 0 dB by 160 ms. |
| Two 16-bar takes in a row, FL's web view stopped | Both passed (65 s and 66 s each); FL answered 30 of 30 pings after each. |
| The same with FL's web view busy | FL stopped answering when the second take started, twice in listening-runner takes (its window stopped repainting at that moment, no dialog), and was busy for 24 s once in a direct run. On a fresh restore, the recipe as the `run` description gives it, run through `run` right after the notes were written, got no answer to the recording's start and FL then stopped answering. Stems took 300 s instead of 130 s. |
| Synth Bass | Its plucky patch decays within the beat, so its dip cannot be told from its own envelope; not measured. |

FL keeps the recorded moves about 0.1 s later than the song position the controller reads while recording.
The playback check cannot see this, because it reads FL's value at the same lagging position.
So the recipe starts each dip 0.1 s early; on a machine with a shorter audio buffer the lag may be shorter, and the dip would then start slightly before the kick.

## The run kernel's start (#324)

| Observation | Value |
| --- | --- |
| First start after the restore, kernel below normal priority | ready 162 s after the server asked for it (granting the runtime included) |
| A later start, FL's web view busy, kernel below normal | the kernel used 0.56 s of CPU in 5 minutes and missed even a 300 s limit |
| Fresh restore, this build (kernel normal) | first cell ran; ready 132 s after the server asked for it (granting included) |
| Same box, web view busy, this build | granting the runtime 89.6 s (the server, below normal), the kernel's own start 4.9 s |
| CPU while it starved | 100 %: FL's web view (`msedgewebview2`) about 62 %, the desktop compositor 15 %, FL 10 %, Defender 5 % |
| Priority of the listening runner and the arena executor (scheduled tasks) and everything they start | below normal |

Windows runs a below-normal process only when nothing at normal priority wants the CPU, apart from a short boost every few seconds.
The kernel's job forced below normal, so while FL's web view kept both vCPUs busy, the kernel's start (interpreter and imports) barely ran.
The kernel now runs at normal priority, fixed by its job so model code cannot raise it; FL's audio threads run above normal.
The server starts the kernel when it starts, in the background, and a start gets a cell's 300 s.

Granting a new environment's runtime to the kernel (the server walks every file's access list) still runs at the server's priority, so on a box whose server runs below normal it delays the first cell; it is not part of the start's limit.
Also seen: a first read of the box's disk is slow (its blocks come from the image's snapshot), and the listening client's own imports took 18 minutes at below-normal priority while the web view was busy.

## For the box image (arena)

- Run the executor's scheduled task at normal priority (Task Scheduler's default, 7, is below normal), so the engine it starts is not starved either.
- FL's web view kept both vCPUs busy for the length of a take; closing or disabling it is what made the measurements above possible.
- A Defender exclusion for the engine's uv cache and a pre-read (or Fast Snapshot Restore) of the restored volume would shorten the first start further; not measured.

## Not verified

- FL for macOS.
- The 0.1 s lag on other audio drivers or buffer sizes.
- Automating channel volume instead of a mixer track for pumping.
- The freeze with FL's web view busy: its cause inside FL was not isolated.
