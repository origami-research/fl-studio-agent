# Arranging the playlist and automating controls, live (2026-11-09)

**Scope.** Before `fl.new_pattern`, `fl.rename_pattern`, `fl.place` and `fl.automate` were built, every way FL offers to put a pattern clip in the playlist or move a control over time was checked live.
The runs used FL Studio 26.1.6.5639 (trial) for Windows on fl-mcp-03, controller API 45, the engine's agent project, and the FLMCP controller.
Only documented FL surfaces were used: the MIDI controller scripting API and FL's own recording, as its manual describes it.

## What FL offers

| Way | Result |
| --- | --- |
| A controller call that adds, moves or removes a playlist clip | **None.** Every public name of FL's `playlist` (42), `patterns` (31) and `arrangement` (12) modules was listed live with `dir()`. Beyond the IL API stubs (API 36) they add only `playlist.selectTool`, `patterns.clearPattern`, `clonePattern(source, dest)`, `setPatternLength`, `incrementPatternLength`, `movePattern`, `duplicatePatternData`, `setLoopStarterRootNote` and the undocumented `arrangement.selectionSet`, `selectionClear`, `selectionIsActive`. FL's online scripting manual (through API 45) lists the same. |
| `patterns.findFirstNextEmptyPat(flags, x, y)` with a position | Selects the next empty pattern; no clip appeared for `x, y`. |
| A plugin host call (`FHD_*`) | Only `FHD_LoadAudioClip` (42) puts a clip in the playlist, and only an audio file at the playlist selection. No host call writes a pattern clip or an automation point; the undocumented ids 88-109 were not touched. |
| A partial project reload | FL has none; a forum thread (March 2026) confirms there is no way to import one project's arrangement into another. |
| **FL's recording, in song mode** | **Works.** FL's manual: recorded control moves go into the selected pattern ("Mouse & Controller Recording"), and in song mode "the Pattern Clip will be automatically placed at the point where Playback started from after recording is stopped" ("Note / MIDI Recording"). |

## What the recording does, live

- `general.processRECEvent(event, value, midi.REC_MIDIController)` once per controller idle tick, while FL records and plays in song mode, kept the moves in the selected pattern, and FL added that pattern's clip at the bar playback started from.
  A 2-bar ramp of the Pad channel's volume from 0.05 to 1.0 played back at 0.057, 0.31, 0.505, 0.755 and 0.975 along the way (the straight line: 0.05 + 0.95 x fraction, each within 0.01).
- A second recording over the same span of the pattern replaced the moves there; a recording over only its first bar replaced only that bar.
- FL adds a clip only when something was recorded: a recording that kept nothing placed nothing, whether the pattern was empty or held notes.
  So a placement records a control that changes nothing heard: the mix level of the FL MCP effect (it passes audio through unchanged), at the value it already has.
  That placed a pattern of notes with its whole length (8 beats).
- FL adds a pattern's clip only where no clip of that pattern starts at or before playback's start.
  With a clip at bar 9, bar 5 was added and bar 17 was not; across the runs 11 of 11 placements with no earlier clip of their pattern were added, and 4 of 4 after an earlier clip were not.
  A copy (`patterns.clonePattern(source, dest)`, which FL named "Bass verse #2") was added after the original's clip.
- FL chose each clip's track (the next playlist track down each time); selecting a playlist track first changed nothing seen.
- The song position stops at the song's end: on an empty song `transport.setSongPos` to bar 9 left FL at bar 1.
  `arrangement.addAutoTimeMarker` at bar 9 extended the song, after which FL moved there; `arrangement.currentTime` follows the playhead past the song's end while `getSongPos` stays at the end.
- `transport.record()` and its state are a tick apart: `isRecording()` read 0 right after arming and 1 a second later, so the controller waits for each change before the next step.

## FL's record question

On a fresh install FL asks "What would you like to record?" as a modal the first time it records.
Raised from inside the controller's MIDI callback, it froze FL twice (Windows showed FL's window as not responding); raised from the controller's idle callback, FL waited for an answer and stayed responsive.
Answering "Notes and automation" with "Don't show this in the future" ticked wrote `RMC\PopupRec=1` (and `General\RecordingFilter2=3`) under `HKCU\Software\Image-Line\FL Studio 26` at once, and FL recorded without asking in every later run, across restarts.
So the controller records only from its idle callback, and on Windows the engine reads `PopupRec` and refuses with the steps until it is 1.

## Reading FL back

- FL has no clip getter.
  `playlist.getTrackActivityLevel` reported the track of a clip of notes while FL played (Bass and Lead clips), but not a drum clip on one run and never an automation-only clip, and it reports nothing while stopped.
  A placement reports the track FL newly shows playing at that bar when there is exactly one, and `None` otherwise.
- `device.getLinkedValue(event)` read the normalized value of every control tried: channel volume (0.78), a FLEX parameter (0.5, as `plugins.getParamValue`), a Maximus parameter on the master (0.302, as `plugins.getParamValue`) and a mixer track's volume (0.8).
  An automation take plays its span back and compares these readings with the line asked for.
- In the first proof take, FL's controller stopped answering for good right after the first automation's playback check, while the screen was being recorded: its status then carried every tick's sample (about 400) and was asked for five times a second as FL played. The same calls passed without the recording. The playback now keeps at most 64 evenly spaced samples and sends them once, at its end, and the engine asks twice a second.
  Later found (#328): the cause was the controller answering from FL's MIDI thread, which can deadlock with FL's main thread; more frames made it likelier ([note](controller-deadlock-2026-11-12.md)).

## Event ids

`channels.getRecEventId(ch, 1) + REC_Chan_Vol | REC_Chan_Pan | REC_Chan_Plugin_First + param`, `mixer.getTrackPluginId(track, 0) + REC_Mixer_Vol | REC_Mixer_Pan`, and `mixer.getTrackPluginId(track, slot) + REC_Plug_Plugin_First + param | REC_Plug_MixLevel` each read the value the matching getter did.

## Limits

- Clips cannot be moved or removed live: no call does it, and FL's playlist edit commands act on a selection the API cannot make.
  Whether `fl.undo()` takes back a placement was not checked, and the session's list of added clips does not follow an undo.
- Automation is kept in patterns, FL's "event" automation, not as automation clips; FL converts one into the other only from its event editor menu.
- Recording is real time: a placement plays about a beat, an automation take plays its span twice (record, then check).
- Not verified: FL for macOS (where it keeps the record question's answer is unchecked, so the engine does not check it there and a recording that FL holds for the question times out), automating a control while the person records, and FL's count-in before recording.
