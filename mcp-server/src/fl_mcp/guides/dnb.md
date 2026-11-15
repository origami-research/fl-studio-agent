---
name: dnb
description: Drum and bass two-step at 174, sub and reese bass, bar map.
---

Use for drum and bass, liquid, neurofunk and jungle-leaning briefs.
Numbers here are genre conventions to start from; a brief or a reference track overrides them.

## Tempo and feel
Drum and bass sits at about 170 to 176 BPM, 174 being the most common.
The two-step beat drives it; the bass moves at half that speed, so the track feels fast and heavy at once.

## Key and harmony
Minor keys.
Liquid uses lush seventh chords and pads (`theory.chord(root, "min7")`); neurofunk uses few chords and lets the bass lead.

## Drums (16 steps a bar; each drum channel is a sampler on MIDI 60)
- Kick: `x.........x.....` (beat 1 and the "and" of 3: the two-step).
- Snare: `....x.......x...` (beats 2 and 4), the loudest drum in the mix.
- Ghost snares: `.......x......x.` at low velocity (about 0.3), for roll.
- Hat or ride: eighths `x.x.x.x.x.x.x.x.` or shuffled sixteenths.
- Breakbeats (the Amen and its relatives) are the jungle sound: search "breakbeat drum loop" and chop it (`fl.guide("chops")`).

## Bass
Two layers: a sub (a plain sine-like tone, roots held long, mono) and a mid bass above it (a reese of detuned saws, or a growl) that carries the movement.
High-pass the mid bass so only the sub holds the lowest octave.
Synth 1 and Synth 2 are spare synths for this: load a preset into each (`fl.guide("sounds")`): "deep sub bass sine", "reese bass detuned", "neuro growl bass".
Liquid often uses one warm rolling bass instead; Synth Bass or Bass works there.

## Other sounds
"atmospheric pad", "amen break", "crisp dnb snare", "ride cymbal", "vocal chop", "impact hit".

## Bar map (about 4 minutes at 174 BPM; DJs mix in 16- and 32-bar phrases)
- Intro 1-32: atmosphere and broken drums, no full bass; a riser over the last 8.
- Drop 33-64: full drums, sub and mid bass.
- Breakdown 65-96: drums and bass out or filtered; pads and melody.
- Build 97-104: the drums return with a roll, the filter opening.
- Drop 2 105-136: the drop with a new bass variation.
- Outro 137-168: drums and atmosphere for the mix-out.
A 32-bar section lasts only 44 seconds at 174, so the long phrases do not feel long.

## Mix
The snare cuts through everything; the kick is short.
Sub mono and steady in level; the mid bass may move and widen.
Hats and ride bright; the pads behind, high-passed.
Duck the bass under the kick when they clash (`fl.guide("sidechain")`); check kick against sub with `fl.guide("listening")`.
Loudness target and the revise loop: `fl.guide("revise")`.

## Common mistakes
- A weak snare, buried by the bass.
- A reese with its own sub fighting the sub layer.
- A drop with no breakdown before it, so it cannot land.
- Eight bars repeated unchanged for a whole 32-bar drop: vary the drums or the bass every 8.

```python
import pyloudnorm as pyln
fl.set_tempo(174)
d = lambda steps, v=0.85: theory.drums(steps * 4, key=60, velocity=v)
fl.track("Kick").write(d("x.........x....."), mode="replace")
fl.track("Snare").write(d("....x.......x...", 0.95) + d(".......x......x.", 0.3), mode="replace")
fl.track("Hat").write(d("x.x.x.x.x.x.x.x.", 0.55), mode="replace")
sub = fl.sounds("deep sub bass sine", kind="preset", limit=3)[0]
synth = fl.load_preset("Synth 1", sub).channel        # FL renames the channel after the preset
roots = [38, 38, 34, 36]                                  # D2 D2 Bb1 C2, a bar each
fl.track(synth).write([Note(r, b * 4, 3.75, 0.8) for b, r in enumerate(roots)], mode="replace")
r = fl.render("dnb.wav", bars=4)
x, sr = fl.read_wav(r)
print(fl.meters(bars=4).summary, round(pyln.Meter(sr).integrated_loudness(x), 1), "LUFS")
```
