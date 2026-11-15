---
name: house
description: House and deep house tempo, drums, bass, chords and bar map.
---

Use for house, deep house, tech house and garage-leaning briefs; techno and disco share the drums but not the harmony habits.
Numbers here are genre conventions to start from; a brief or a reference track overrides them.

## Tempo and feel
House usually sits at 120 to 128 BPM, deep house at the slow end (about 118 to 124).
The groove comes from swung sixteenths and the off-beat open hat; straight, equal-velocity hats sound mechanical (swing and accents: see `fl.guide("arrangement")`).

## Key and harmony
Minor keys are the default for deep house; seventh and ninth chords (m7, m9, maj7) give its warmth.
`theory.chord(root, "min7")` gives a seventh; add the root plus 14 semitones for a ninth.
Two to four chords, one per bar or two, played as held pads or short off-beat stabs.

## Drums (16 steps a bar; each drum channel is a sampler on MIDI 60)
- Kick: `x...x...x...x...` (four on the floor).
- Clap or snare: `....x.......x...` (beats 2 and 4).
- Open hat: `..x...x...x...x.` (the off-beat, the house signature).
- Closed hat: `x.x.x.x.x.x.x.x.` or all sixteenths with the off-steps softer.
- Perc or shaker: a syncopated line such as `...x..x....x..x.`, low in the mix.

## Bass
Keep the bass off the kick: notes on the off-beats or in the gaps between kicks, short, mostly roots with octave and fifth moves.
Play it in the sub-and-bass range, roughly MIDI 33 to 48, on Synth Bass (house) or Bass (warmer, deep house).
Duck it and the pad under the kick with `fl.guide("sidechain")`.

## Sounds to search
"round soft deep house kick", "tight closed hi-hat crisp", "dry rimshot", "house clap", "warm Rhodes chord stab", "dusty organ chord stab", "lush detuned pad", "shaker loop"; choose by ear with `fl.guide("sounds")`.

## Bar map (about 3 minutes at 124 BPM)
- Intro 1-16: drums only, adding hats and perc every 4 to 8 bars (DJs mix over this).
- Groove 17-32: bass and chords enter.
- Breakdown 33-40: kick and bass out; pad carries the chords, a low-pass opening across it.
- Build 41-48: riser, snare roll, filter fully open by the last bar, a beat of silence before 49.
- Drop 49-80: everything, plus a stab or hook; vary every 8 bars.
- Outro 81-96: strip back to drums for the mix-out.

## Mix
The kick and bass own the lows (below roughly 100 to 150 Hz, a rule of thumb); high-pass the pads, keys and hats.
Hats bright but quieter than you think; clap with a short reverb; pads behind the stab.
Loudness target and the revise loop: `fl.guide("revise")`.

## Common mistakes
- Bass notes on the kick, so the lows blur and pump unevenly.
- No breakdown: 64 bars at one energy sounds like a loop.
- The same four chords and hat pattern unchanged for the whole track.
- Hats too loud and dry, every hit at one velocity.
- A muddy midrange from a pad, Rhodes and stab all playing the same octave.

```python
import pyloudnorm as pyln
fl.set_tempo(124)
d = lambda steps, v=0.8: theory.drums(steps * 4, key=60, velocity=v)   # 4 bars
swing = lambda notes, pct=56: [n.model_copy(update={"start": n.start + (pct - 50) / 200}) if round(n.start * 4) % 2 else n for n in notes]
fl.track("Kick").write(d("x...x...x...x..."), mode="replace")
fl.track("Clap").write(d("....x.......x..."), mode="replace")
fl.track("Open Hat").write(d("..x...x...x...x.", 0.55), mode="replace")
fl.track("Hat").write(swing([n.model_copy(update={"velocity": 0.75 if i % 2 == 0 else 0.5}) for i, n in enumerate(d("xxxxxxxxxxxxxxxx"))]), mode="replace")
am9 = [*theory.chord("A3", "min7"), 57 + 14]                             # A C E G, then B a ninth up
fl.track("E-Piano").write([Note(k, b * 4 + 1.5, 0.5, 0.6) for b in range(4) for k in am9], mode="replace")
fl.track("Synth Bass").write([Note(33, b + 0.5, 0.35, 0.8) for b in range(16)], mode="replace")
r = fl.render("house.wav", bars=4)
x, sr = fl.read_wav(r)
print(fl.meters(bars=2).summary, round(pyln.Meter(sr).integrated_loudness(x), 1), "LUFS")
```
