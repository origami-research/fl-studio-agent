# Sidechain, effect parameters and samples made in Python, live (2026-11-09)

**Scope.** Three producer needs: the kick ducking bass and pads, samples used as audio (one-shots, loops), and chopping or stretching a sample.
Every call below is a documented surface: the controller API through the FLMCP controller, and the Bridge's `load_in_channel` (`FHD_LoadInChannel`, plugin SDK id 43).
Runs used FL Studio 26.1.6.5639 (trial) for Windows on the dev box fl-mcp-04, controller API 45, the packaged agent project, and renders measured in numpy.
Probe scripts and their JSON and WAV evidence are kept outside the repository (`~/.cache/sidechain` on the operator's Mac).

## Results

| Need | What was tried | Live answer |
| --- | --- | --- |
| A mixer effect's parameters | `plugins.getParamCount/getParamName/getParamValue(track, slot)` on Fruity Limiter, Fruity Compressor, Gross Beat, Fruity Peak Controller, Reeverb 2 | Each lists its parameters (Limiter 18, Compressor 6, Gross Beat 9, Peak Controller 10). |
| Set one | `plugins.setParamValue(v, i, track, slot)` | Read back exact: Limiter "Comp threshold" 0.05, "Comp ratio" 1.0; Compressor threshold 0.25, ratio 1.0, release 0.3. |
| FL's text for a value | `plugins.getParamValueString(i, track, slot)` | "-35.4dB", "20.0:1", "14.92ms", "-45.0 dB", "30.0 : 1", "1201 ms", "Hard"; a FLEX channel's "-65%"; 18 Limiter names, values and texts took 3.2 s. The engine had appended the global-index flag as a fifth argument, which FL refused ("function takes at most 4 arguments"); the flag goes after the slot. |
| A compressor's sidechain input | Read every parameter | None is the sidechain selector, on Fruity Limiter or Fruity Compressor; FL sets it only in the plugin window (right-click the SIDECHAIN box), and a 2025 beta forum post relays Image-Line's developers finding it problematic to automate. |
| Key one through presets | A level-0 send from an inaudible kick track to the pad's track (`fl.send(kick, pad, level=0)`, FL's sidechain link), then the stock "Fruity Limiter/Ducking music behind voice" (threshold set to -35.4 dB, 20:1), or "Fruity Compressor/External/Default" (-45 dB, 30:1) | No ducking: the pad's beat-folded envelope varied 2 to 4 dB with or without the link and with the kick muted, and the compressor squashed the pad by its own level (-54 dB in both renders). Neither preset selects the link. |
| The template's sidechain | The kick into the "SideChain" track (124), which FL's Trap template links at level 0 to the Sidechained Bus (123) and Sub Bus (125), each with a Fruity Limiter; the pad into the Sidechained Bus | No measurable ducking (beat-folded depth 5.1 dB keyed against 2.3 dB with the kick muted, no dip after the kick). The palette guide's "ducked by the kick" does not hold on this build. |
| A sample FL never shipped | `load_in_channel` with a WAV outside FL's folder | Loaded; the sampler was renamed "My Chop" (then "My Chop #2" on a second channel). |
| A note's length | The 1.5 s chop on a 0.25-beat note | On "Sampler 2" the whole sample played; on "Sampler 1" it stopped about 0.2 s in and decayed. Channels differ, so a note as long as the sample plays it whole on any of them. |
| Sampler settings | `channels.getRecEventId` plus `REC_Chan_SmpOfs` (13) and `REC_Chan_StretchTime` (14), read with `channels.incEventValue(id, 0)` | Values only (16384, 36864); FL documents no reverse, loop, keep-length or stretch-mode event. |

## What this means

- **Sidechain from a compressor: impossible through the documented API.**
  The sidechain input is a plugin-window setting, not a parameter, and the stock presets tried do not set it.
  Peak and Formula Controllers need FL's "Link to controller" dialog, which has no API.
  Hand-editing a plugin's state to set it would be per-plugin reverse engineering, which the project does not do.
- **Pumping is still possible as audio.**
  Render the part alone (`fl.render(..., solo=[part])`), multiply it by a gain dip after each kick in numpy, write the WAV, load it into a spare sampler, and clear the part's notes.
  It is printed audio: a change to the part's notes needs another bounce.
- **Samples are audio the model shapes in Python.**
  Chop, rearrange, reverse, stretch (`librosa.effects.time_stretch`) and re-pitch (`pitch_shift`) a stock sample, write a WAV in the workspace, and `fl.load_sample` it.
  Length and tempo are Python too (`len(x) / sr`, `librosa.beat.beat_track`).
  Slicex and Edison were not needed.
- **Effect parameters, with FL's own text, are live** (`fl.effect(track, slot)`).

## Also observed

- With the Ducking limiter on the pad's track (its peak window 216 ms), sampler channels sounded 0.22 to 0.23 s into each render, where the FPC kick had sounded at 0.015 s before it was loaded.
  This is most likely FL's plugin delay compensation delaying the mix by the limiter's lookahead while the render starts on time; the cause was not isolated.
- FL opened each loaded effect's window, stacking them over the mixer.

## Not verified live

- FL for macOS.
- Whether any other stock preset of a sidechain-capable effect selects a sidechain input.
- Pitch of a sample on keys other than MIDI 60.
- Pumping drawn with `fl.automate` (merged the same day) on the ducked track's mixer volume, a live alternative to the bounce.
