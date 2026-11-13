# FL stopped answering: the controller answered from FL's MIDI thread (2026-11-12, #328)

**Scope.** `fl.automate` froze FL five times in the Strudel experiment (engine 0.1.9), including the pumping recipe from the `run` tool's own docstring.
This note records how the freeze was reproduced, what a frozen FL was doing, and the fix (controller 0.2.9).
Logs, diagnostics and replays are kept outside the repository (`~/.cache/issue328` on the operator's Mac).

## What a frozen FL was doing

Each frozen FL kept its window responsive (Windows reported it responding, about 20 % of one core), FL's web view was not running, and both loopMIDI ports passed a test message from a separate process, yet FL sent nothing back to a ping.
`py-spy dump` of FL's embedded Python, in two frozen replays (a third dump could not attach):

| Thread | Python | Native |
| --- | --- | --- |
| FL's MIDI input thread | `OnSysEx` → `_send_response` → `_send_frames` → `device.midiOutSysex` | `WaitForSingleObjectEx` inside FLEngine |
| FL's main thread | none | `DispatchMessageW` → FL's window procedure → winmm's MIDI input (`wdmaud.drv`) → `RtlEnterCriticalSection` |

So FL calls `OnSysEx` on its MIDI input thread, and a response sent from there waits for FL's main thread, while the main thread waits on a lock in the MIDI input path.
Neither moves again: a deadlock, not a busy FL.
Any request can hit it; the more frames a request or response takes and the busier the main thread (playback, a recorded screen), the likelier.
That is why it struck automation takes (a pumping take sends about 190 points in one request), renders (a solo render's mute ops) and meters, and why the traffic cuts of #311, #318, #321 and #325 made it rarer without ending it.
The earlier attribution to FL's web view (live-pumping note, 2026-11-10) was a load factor at most: here the web view was off.

## Reproduction, before the fix (engine 0.1.9, controller 0.2.8)

| Replay | What happened |
| --- | --- |
| A take (house, pumping recipe) | FL stopped answering during the stems render, before the automation |
| C take (house, filter sweeps) | one take passed, then FL stopped answering at the next take's first read |
| C take (phonk, filter sweeps) | two takes passed, then FL stopped answering at the next `ctl.record` |
| C take (house, first try) | FL never answered: the request port no longer delivered even a test message, after the previous frozen FL had been killed |

3 of 3 replays that started froze, after 3 automation takes in all.

## The fix (controller 0.2.9)

- `OnSysEx` only reassembles a request and queues it; it calls no FL function and sends nothing.
- `OnIdle`, on FL's main thread, runs the queued requests in order and sends their responses, then retries deferred operations and steps a recording job.
- Each request carries how long the engine waits for it (`wait`); one still queued after that (FL does not call `OnIdle` under a modal dialog) is dropped, so a change the engine already reported as failed cannot land late or twice.
- The engine cancels a recording job whose status FL stops answering.
- Every live request (read, write, and the ping that confirms the controller is there) waits 8 s instead of 1 to 2 s; see below.

## After the fix


| Replay | Automation takes (xlarge / large) | Renders (xlarge / large) | FL stopped answering |
| --- | --- | --- | --- |
| A take (pumping), with three more pumping takes | 4 / 4 (16 bars, 190 points each) | 2 / 2 (one of stems) | never |
| C take (house, filter sweeps) | 15 / 15 | 9 / 9 | never |
| C take (house, first try) | 15 / 15 | 2 / 2 | never |
| C take (phonk) | 7 / 7 | 4 / 4 | never |
| C take (phonk, retry) | 7 / 7 | 5 / 5 | never |

96 of 96 automation takes passed the playback check (largest `max_error` 0.0005; the check allows 0.03) and 44 of 44 renders succeeded; no server log of the final code has an "FL is busy" line.

A second client pinged the controller every 0.2 s during a take:

| --- | --- | --- | --- | --- | --- |
| 1 s ping, 2 s reads | 614 | 0.10 s | 1.8 s | 3.7 s | 25 |
| final (8 s) | 740 | 0.09 s | 1.1 s | 3.1 s | 16 |

So FL's main thread stalls for seconds on a loaded 2-vCPU machine, and the engine's 1 s ping and 2 s reads, sized for answers from the MIDI thread, were too short.
Every live request now waits 8 s, as writes already did.
An 8-bar automation take took 36 to 40 s instead of about 33 s.

## Not verified

- FL for macOS: which thread it calls `OnSysEx` on was not checked; the fix does not depend on it.
- The native frames are the nearest exported symbols (FL ships no symbols), so the exact lock is inferred, not named.
