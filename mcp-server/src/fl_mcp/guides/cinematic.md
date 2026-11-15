---
name: cinematic
description: Orchestral cues with strings, brass, choir and CC0 drums.
---

Use for cinematic, orchestral, epic, trailer and film-score briefs; for ambient pads without an orchestra the harmony advice still applies.
Numbers here are conventions to start from; a brief or a reference overrides them.

## Tempo and feel
Tempo follows the scene: about 60 to 90 BPM for emotional cues, about 120 to 150 for action ostinatos.
An orchestra breathes: dynamics, not a constant level, make it sound real.

## Key and harmony
Minor keys and modes (Aeolian, Dorian) are the default.
The epic progression i-VI-III-VII (A minor, F, C, G) and slow two-chord alternations both work.
Hold a pedal note in the low strings under changing chords to build tension; suspended chords that resolve release it.
Keep melody, harmony and bass in separate registers: low strings and brass below about MIDI 50, chords in the middle, melody on top.

## The orchestra in the starting project
Strings (a sustained section, MIDI 36 to 96), Brass, Woodwinds, Choir and Piano are sampled instruments ready to play.
The CC0 packs add percussion and articulations, found with `fl.sounds` like any sound (`fl.guide("sounds")`): "orchestral bass drum big hit", "gong hit", "timpani", "cymbal swell", "violins spiccato", "violins pizzicato", "large drum mallet".
Load them on spare samplers or drum channels; FL renames a channel after what it loads, so keep the returned channel index.
A pitched CC0 sample is one note: `Sound.pitch` says which, and a sampler plays it at that pitch on MIDI 60, so write `60 + wanted - pitch` and stay near its own pitch, because far transposition sounds unnatural (re-pitching properly: `fl.guide("chops")`).

## Percussion (16 steps a bar)
- Big drum: `x.......x.......` in a build, growing to `x..x..x.x..x..x.` (a 3-3-2 pulse) at the climax.
- Snare or toms: rolls at rising velocity into section changes.
- Gong or crash: once, on the climax's first beat.
- Cymbal swell: a riser ending exactly on that beat.

## Building a cue
Add one layer every 4 to 8 bars: pad or sustained strings, then an ostinato, then low brass, then percussion, then choir and the full melody.
Rising velocity is the crescendo for sampled parts; draw a volume swell on a sustained part with `fl.automate` on its channel volume.
Short notes (spiccato, pizzicato) make the pulse; long notes make the size.

## Bar map (about 2 minutes at 90 BPM)
- Intro 1-8: one sustained string chord or a solo piano or woodwind line, quiet.
- Build 9-24: the ostinato enters, then low brass and big drums, one layer every 4 bars.
- Climax 25-40: everything: brass melody, choir, full percussion, the gong on bar 25.
- Resolution 41-48: back to strings or piano, the last chord ringing out.

## Mix
Contrast is the point: the intro should be much quieter than the climax, so do not even it out.
More reverb than pop: the Reverb Send already takes the orchestral tracks (see `fl.guide("mixing")` for the send levels).
Percussion in front, strings and choir behind; no pumping.
Loudness target and the revise loop: `fl.guide("revise")`; reach the target with the climax, not by limiting the quiet parts up.

## Common mistakes
- Every part playing from bar 1 at the same velocity.
- Several low parts stacked in the same octave: a muddy roar instead of weight.
- The melody in the same register as the chords, so it disappears.
- Samples played far from their recorded pitch.

```python
import numpy as np
fl.set_tempo(90)
drum = max(fl.sounds("orchestral bass drum big hit", kind="sample", limit=4), key=lambda s: s.low or 0)
big = fl.load_sample("Sampler 3", drum).channel     # FL renames the channel after the sample
am, f, c, g = (theory.chord(r, q) for r, q in (("A3", "minor"), ("F3", "major"), ("C4", "major"), ("G3", "major")))
fl.track("Strings").write([Note(k, b * 4, 4, 0.35 + 0.05 * b) for b, ch in enumerate([am, f, c, g] * 2) for k in ch], mode="replace")
fl.track("Brass").write([Note(r, 16 + i * 4, 4, 0.55 + 0.05 * i) for i, r in enumerate([45, 41, 48, 43])], mode="replace")  # enters bar 5
hits = theory.drums("x.......x......." * 4 + "x..x..x.x..x..x." * 4, key=60, velocity=0.9)
fl.track(big).write(hits, mode="replace")
fl.automate([{"position": 0, "value": 0.4}, {"position": 32, "value": 0.85}], bar=1, channel="Strings", param="volume")
x, sr = fl.read_wav(fl.render("cue.wav", bars=8))
bar = int(sr * 240 / 90)
rms = [20 * np.log10(np.sqrt(np.mean(x[i * bar:(i + 1) * bar] ** 2)) + 1e-9) for i in range(len(x) // bar)]
print("level per bar (dBFS):", [round(float(v), 1) for v in rms])
```
