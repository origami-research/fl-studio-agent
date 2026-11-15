---
name: phonk
description: Drift phonk cowbell riffs, distorted 808s, drums and bar map.
---

Use for drift phonk and phonk briefs; for Memphis-era lo-fi phonk lean on `fl.guide("lofi")` for the texture and `fl.guide("trap")` for the drums.
Numbers here are genre conventions to start from; a brief or a reference track overrides them.

## Tempo and feel
Drift phonk mostly sits around 120 to 140 BPM, many tracks near 130, written in full time (snare on 2 and 4).
It is aggressive and repetitive by design: a one- or two-bar riff carries the track, so energy has to come from layers and sections, not new melodies.

## Key and harmony
Minor keys; the riffs often lean Phrygian, using the flat second (`theory.scale(root, "phrygian")`).
Chords are rare; the cowbell riff and the 808 roots imply the harmony.

## Drums (16 steps a bar; each drum channel is a sampler on MIDI 60)
A starting pattern; drift phonk drums vary, so match the reference when there is one.
- Kick: `x.....x...x.....`, with the 808 on the same hits.
- Clap or snare: `....x.......x...`, loud and bright.
- Hat: `x.x.x.x.x.x.x.x.`, sixteenth bursts at phrase ends.
- Open hat or crash on the first beat of each section.

## The 808
The 808 channel is a tuned FLEX 808 already into Fruity Fast Dist and Fruity Soft Clipper, so it arrives distorted.
Play roots in MIDI 24 to 48, one note at a time, following the kick's rhythm.
Tune it to the key: an 808 a semitone off the riff is the most common phonk fault.
Keep the kick short and punchy so it cuts through the 808's sustain, or duck the 808 with `fl.guide("sidechain")`.

## The cowbell
The Cowbell channel is a pitched 808 cowbell: it plays any note, natural pitch at MIDI 60, and its mixer track has a short delay.
Write the riff in the key's scale, mostly eighths and sixteenths, in the octave above MIDI 60.
Search for alternatives with `fl.guide("sounds")`: "808 cowbell", "distorted cowbell", "dark vocal chop", "hard phonk clap", "aggressive distorted 808".

## Bar map (about 2 minutes at 130 BPM)
- Intro 1-8: the riff alone or with hats, filtered; the 808 enters in the last bar.
- Drop 9-24: kick, 808, clap, riff at full energy.
- Break 25-32: kick and 808 out or sparse; the riff with a filter or delay throw.
- Drop 2 33-48: everything back, plus a counter-riff or extra percussion.
- Outro 49-56: strip back, or end hard on the first beat of 57.

## Mix
The 808 and kick share the lows: never both sustaining at once.
Distortion adds loudness: a mix that already sounds crushed before the master's Maximus loses the kick's punch, so turn the 808 down rather than up.
The cowbell sits on top and bright; if the distortion makes it harsh, cut around 2 to 5 kHz on its track.
Loudness target and the revise loop: `fl.guide("revise")`.

## Common mistakes
- The 808 or the riff out of key.
- Long kick and long 808 together, so the lows are mud.
- Every part distorted: harsh, small and over-crushed rather than heavy.
- No break, so the riff loops unchanged for two minutes.

```python
import numpy as np
fl.set_tempo(130)
d = lambda steps, v=0.85: theory.drums(steps * 4, key=60, velocity=v)
kick = d("x.....x...x.....")
fl.track("Kick").write(kick, mode="replace")
fl.track("Clap").write(d("....x.......x..."), mode="replace")
fl.track("Hat").write(d("x.x.x.x.x.x.x.x.", 0.55), mode="replace")
roots = [37, 37, 38, 40]                                   # C#2 C#2 D2 E2, one per bar
fl.track("808").write([Note(roots[int(n.start // 4)], n.start, 0.9, 0.9) for n in kick], mode="replace")
riff = theory.scale("C#4", "phrygian")                     # C# D E F# G# A B
steps = [0, 0, 1, 0, 4, 3, 1, 0]
fl.track("Cowbell").write([Note(riff[s], i * 0.5 + b * 4, 0.4, 0.8) for b in range(4) for i, s in enumerate(steps)], mode="replace")
x, sr = fl.read_wav(fl.render("808.wav", bars=1, solo=["808"]))   # bar 1 holds C#2 only
spec, f = np.abs(np.fft.rfft(x.mean(axis=1))), np.fft.rfftfreq(len(x), 1 / sr)
low = (f > 20) & (f < 200)
midi = 69 + 12 * np.log2(f[low][np.argmax(spec[low])] / 440)   # distortion may make an octave the peak
print("808 is", round(float((midi - 37 + 6) % 12 - 6), 2), "semitones from C#")
```
