---
name: mixing
description: Balance a mix with levels, EQ, reverb and delay sends, panning.
---

Mix in this order, because each step changes what the next one needs: balance, cuts, space, movement, then the master.
Make one change, render, listen and measure (`fl.guide("listening")`), then the next.

## Balance first
Pick the anchor: the kick in dance music and hip hop, the lead or the main chord instrument elsewhere.
Bring every other part up against it until it is clearly heard but does not cover the anchor; most problems that sound like EQ problems are level problems.
Set channel levels with `fl.track(ch).set_levels`; `fl.channels` and `fl.mixer` show where each part plays.
The 808 track starts lower than the rest because its distortion makes it much louder; keep it that way unless a render says otherwise.

## Headroom and the master
The master ends in Maximus, which keeps the mix loud and unclipped.
Feeding it a mix that is already too loud makes it squash the drums: the result sounds crushed and smaller, not louder.
If a render sounds flat and dense, or its true peak sits right against its loudness, turn the loudest parts down, not the master up.
Compare peak-to-loudness against a reference measured the same way rather than a fixed number (`fl.guide("listening")`).
Streaming services turn loud tracks down to their own level, so extra loudness buys nothing; the target and the loop that reaches it are in `fl.guide("revise")`.

## EQ in plain terms
Rough regions (rules of thumb, not borders): sub below about 60 Hz is felt; 60 to 250 Hz is weight; 250 to 500 Hz is warmth or mud; 500 Hz to 2 kHz is the midrange, boxy or honky when overdone; 2 to 5 kHz is presence and bite; 5 to 10 kHz is cymbal sizzle and sibilance; above about 10 kHz is air.
- Cut before you boost, and cut narrow problems, boost wide.
- High-pass parts that do not need lows (hats, pads, keys, leads, reverb) so the kick and bass own them.
- Muddy or boomy: cut a few dB in the low mids on the pads and keys, not on the bass.
- Harsh: find which part is loud at 2 to 10 kHz and cut there on that part only.
- Dull: brighten the hats or choose a brighter sound (`fl.guide("sounds")`) before boosting the top of everything.
The Bass track and the Drum Bus already hold Fruity Parametric EQ 2 in slot 0; add one elsewhere by loading its effect preset onto a channel with `fl.load_preset`.
Set it with `fl.effect(track, slot).set_param` and read the returned `display` ("120 Hz", "-3.0 dB") to know what you set; `params("freq")` finds the band controls.

## Space: the sends
The project has a Reverb Send (Fruity Reeverb 2) fed by the melodic tracks and a Delay Send (Fruity Delay Bank) fed by Lead and Pluck; find them by name in `fl.mixer`.
Change how much a part goes there with `fl.send(part_track, send_track, level=...)`; more send places a part further back.
Keep the kick, bass and 808 dry: reverb on low end blurs it, and the project does not send them.
Lead and vocals-like parts drier and in front; pads and choirs wetter and behind.
Fast, dense genres want a shorter reverb (set its "Decay time" through `fl.effect` and read the display); ballads and cinematic cues can take a long one.

## Panning
Kick, bass, snare and the lead stay centred (a convention: the low end and the focus belong in the middle).
Spread hats, percussion, doubled parts and counter-melodies a little left and right with `set_levels(pan=...)`, in pairs so the mix stays balanced.

## Movement
Pumping (a pad or bass ducking under the kick) is drawn with `fl.automate`: `fl.guide("sidechain")`.
Volume rides and filter sweeps belong to the arrangement: `fl.guide("arrangement")`.

```python
import pyloudnorm as pyln
track = {c.name: c.mixer_track for c in fl.channels}
send = {m.name: m.index for m in fl.mixer}
for ch, volume in (("Kick", 0.8), ("Synth Bass", 0.62), ("Clap", 0.6), ("Hat", 0.45), ("Pad", 0.42)):
    fl.track(ch).set_levels(volume=volume)
fl.track("Hat").set_levels(pan=0.2)
fl.track("Perc").set_levels(pan=-0.2)
print(fl.send(track["Pad"], send["Reverb Send"], level=0.7))
print([(p.name, p.display) for p in fl.effect(track["Bass"], 0).params("freq")[:3]])
parts = ["Kick", "Synth Bass", "Pad"]
stems = fl.render("mix.wav", bars=2, stems=parts)
lufs = {ch: pyln.Meter(fl.read_wav(s)[1]).integrated_loudness(fl.read_wav(s)[0]) for ch, s in zip(parts, stems)}
print({ch: round(float(v - lufs["Kick"]), 1) for ch, v in lufs.items()}, "LU against the kick")
```
