---
name: pop
description: Pop song form, four-chord harmony and a lead that sings the hook.
---

Use for pop, dance-pop, synth-pop and singer-songwriter briefs; for club-length dance music use `fl.guide("house")`.
Numbers here are genre conventions to start from; a brief or a reference track overrides them.

## Tempo and feel
Pop mostly sits at about 90 to 130 BPM: ballads lower, dance-pop around 115 to 128.
Everything serves the hook: a short, repeated melody that the chorus is built around.

## Key and harmony
Major keys are most common; minor for darker pop.
Four-chord loops carry most songs: I-V-vi-IV (C, G, Am, F) and vi-IV-I-V (Am, F, C, G).
Build each chord with `theory.chord(root, quality)` and invert so the top note moves little.
The chorus melody sits higher than the verse melody; that lift is much of what makes a chorus feel bigger.

## The lead is the voice
There is no singer, so a melodic channel sings the topline: Lead (synth), Piano, Sax or Guitar.
Keep it the clearest thing in the mix and give it space: fewer parts play in its register while it plays.
Write in phrases of 2 or 4 bars with rests between them, as a singer breathes.

## Drums (16 steps a bar; each drum channel is a sampler on MIDI 60)
- Kick: `x.......x.x.....`, or `x...x...x...x...` for dance-pop.
- Clap or snare: `....x.......x...` (beats 2 and 4).
- Hat: `x.x.x.x.x.x.x.x.` in the chorus, sparser or absent in the verse.

## Bass
Roots on the chord changes, its rhythm locked to the kick; Bass (MIDI 36 to 60, silent below 36) or Synth Bass (MIDI 28 to 50).

## Bar map (about 4 minutes at 120 BPM)
- Intro 1-8: the hook's chords or the hook itself, thin.
- Verse 9-24: drums light, bass, chords; the lead's lower verse melody.
- Pre-chorus 25-32: rising: a riser, a snare build, the bass holding or climbing.
- Chorus 33-48: the fullest: the hook up high, pads, octave doubles, a crash on 33.
- Verse 2 49-64 and pre-chorus 65-72: like the first, with one new layer.
- Chorus 73-88.
- Bridge 89-96: new chords or a breakdown; drums out or half.
- Final chorus 97-112: the biggest: an extra layer, or the whole chorus up a semitone or a tone (a classic key change).
- Outro 113-116.
Make each section audibly different in density; render and compare section loudness (`fl.guide("listening")`).

## Mix
The lead on top: present in the 2 to 5 kHz range and not covered by pads or keys there.
Kick and bass solid but supportive; the lows shared cleanly (`fl.guide("sidechain")` if they clash).
Bright but not harsh; reverb and delay on the lead from the sends (`fl.guide("mixing")`).
Loudness target and the revise loop: `fl.guide("revise")`.

## Common mistakes
- A chorus no bigger than the verse.
- The lead buried under chords; check it with a render that mutes it and one that does not.
- A long intro: pop gets to the hook fast.
- Four chords and one drum pattern unchanged for three minutes.

```python
import pyloudnorm as pyln
fl.set_tempo(118)
inv = lambda ch: ch[1:] + [ch[0] + 12]                       # first inversion: the top note moves less
c, g, am, f = theory.chord("C4"), inv(theory.chord("G3")), theory.chord("A3", "minor"), inv(theory.chord("F3"))
fl.track("Piano").write([Note(k, b * 4, 4, 0.5) for b, ch in enumerate([c, g, am, f]) for k in ch], mode="replace")
fl.track("Bass").write([Note(r, b * 4 + s, 0.9, 0.75) for b, r in enumerate([36, 43, 45, 41]) for s in (0, 2, 2.5)], mode="replace")  # C2 G2 A2 F2
d = lambda steps, v=0.8: theory.drums(steps * 4, key=60, velocity=v)
fl.track("Kick").write(d("x.......x.x....."), mode="replace")
fl.track("Clap").write(d("....x.......x..."), mode="replace")
hook = [(76, 0, 1), (74, 1, 0.5), (72, 1.5, 1.5), (79, 4, 1), (77, 5, 0.5), (76, 5.5, 1.5)]   # E D C, G F E
fl.track("Lead").write([Note(k, s + off, l, 0.85) for off in (0, 8) for k, s, l in hook], mode="replace")
r = fl.render("chorus.wav", bars=4)
without = fl.render("chorus-no-lead.wav", bars=4, mute=["Lead"])
lufs = lambda w: pyln.Meter(fl.read_wav(w)[1]).integrated_loudness(fl.read_wav(w)[0])
print("the lead adds", round(lufs(r) - lufs(without), 1), "LU to the chorus")
```
