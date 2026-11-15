---
name: lofi
description: Lo-fi hip hop swung drums, jazz chords, dusty texture.
---

Use for lo-fi hip hop, chillhop and jazzy boom-bap; for hard modern beats use `fl.guide("trap")`.
Numbers here are genre conventions to start from; a brief or a reference track overrides them.

## Tempo and feel
Lo-fi usually sits at about 70 to 90 BPM.
The feel is loose: heavy swing on the hats, varied velocities, a snare that sits slightly late; a perfectly gridded beat sounds wrong here (swing and accents: see `fl.guide("arrangement")`).

## Key and harmony
Jazz harmony: seventh and ninth chords (maj7, min7, dom7, 9ths), often ii-V-I movement, in major keys or Dorian minor.
`theory.chord(root, "maj7")` or `"min7"` gives a seventh; add the root plus 14 semitones for a ninth.
Voice chords in the middle register (around MIDI 48 to 72) and move each voice as little as possible between chords.

## Drums (16 steps a bar; each drum channel is a sampler on MIDI 60)
- Kick: `x.........x.....` (beat 1 and the "and" of 3), with an extra pickup now and then.
- Snare: `....x.......x...` (beats 2 and 4), soft and a touch late.
- Hat: `x.x.x.x.x.x.x.x.`, with eighth-note swing, hard (MPC-style 60 to 66 percent) with velocities between about 0.4 and 0.8.
- Perc: a shaker or rim for a few bars, not the whole track.

## Bass
Round and simple: roots on the chord changes, a passing note into the next chord.
Bass (electric bass guitar, MIDI 36 to 60; it is silent below 36) suits it; a soft sub works for chillhop.
No sub-heavy 808 unless the brief asks; lo-fi bass is warm, not booming.

## Sounds and texture
E-Piano (Rhodes) is the classic chord instrument; Guitar, Piano and Sax add melody.
Search with `fl.guide("sounds")`: "vinyl crackle noise", "dusty boom bap kick", "soft snare", "brushed snare", "tape hiss", "jazz guitar chord".
Dull the top end: lower the instruments' filter cutoff (`fl.track(ch).params("cutoff")`) rather than turning parts down.
A crackle layer runs the whole track, quietly, on a spare sampler.

## Bar map (about 2 minutes 20 at 82 BPM)
- Intro 1-8: chords filtered, crackle, no drums or just hats.
- A 9-24: drums and bass enter; melody phrases answer the chords.
- B 25-32: a change: drums half out, a new melody instrument, or the chords voiced higher.
- A' 33-40: the groove back, a counter-melody added.
- Outro 41-48: drums out, the filter closing, crackle last.
Lo-fi tolerates repetition, but something should change every 4 to 8 bars.

## Mix
Soft and warm rather than loud-sounding: drums tucked behind the keys, the top end rolled off.
The keys and melody are the focus; the kick is felt more than heard.
The Reverb Send already takes the melodic tracks; a little more on the keys suits the genre (see `fl.guide("mixing")`).
Loudness target and the revise loop: `fl.guide("revise")`.

## Common mistakes
- Too clean: quantized, equal velocities, bright cymbals.
- One four-bar loop for three minutes.
- Crackle or hiss as loud as the music.
- A booming sub that belongs to trap.

```python
import pyloudnorm as pyln
fl.set_tempo(82)
swing = lambda notes, pct=62: [n.model_copy(update={"start": n.start + (pct - 50) / 100}) if round(n.start * 2) % 2 else n for n in notes]  # off-eighths
d = lambda steps, v=0.8: theory.drums(steps * 4, key=60, velocity=v)
fl.track("Kick").write(d("x.........x....."), mode="replace")
fl.track("Snare").write([n.model_copy(update={"start": n.start + 0.03}) for n in d("....x.......x...", 0.7)], mode="replace")
hats = swing(d("x.x.x.x.x.x.x.x.", 0.6))
fl.track("Hat").write([n.model_copy(update={"velocity": (0.75, 0.45, 0.6, 0.5)[i % 4]}) for i, n in enumerate(hats)], mode="replace")
nine = lambda root, q: [*theory.chord(root, q), theory.chord(root, q)[0] + 14]
prog = [nine("Eb3", "maj7"), nine("C3", "min7"), theory.chord("Ab3", "maj7"), [56, 62, 67]]  # Bb13 as Ab D G, near the Abmaj7
fl.track("E-Piano").write([Note(k, b * 4, 3.8, 0.55) for b, ch in enumerate(prog) for k in ch], mode="replace")
fl.track("Bass").write([Note(r, b * 4, 3.5, 0.7) for b, r in enumerate([39, 36, 44, 46])], mode="replace")  # Eb2 C2 Ab2 Bb2
r = fl.render("lofi.wav", bars=4)
x, sr = fl.read_wav(r)
print(fl.meters(bars=4).summary, round(pyln.Meter(sr).integrated_loudness(x), 1), "LUFS")
```
