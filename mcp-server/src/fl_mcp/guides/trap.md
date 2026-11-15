---
name: trap
description: Trap half-time drums, hat rolls, long 808s and bar map.
---

Use for trap, drill-leaning and modern hip hop beats; for boom-bap or lo-fi use `fl.guide("lofi")`, for drift phonk `fl.guide("phonk")`.
Numbers here are genre conventions to start from; a brief or a reference track overrides them.

## Tempo and feel
Trap is usually written at about 130 to 170 BPM (140 is common) with a half-time feel: the snare or clap lands once a bar, on beat 3.
The hi-hats carry the speed; the kick and 808 carry the bounce.

## Key and harmony
Minor keys; natural or harmonic minor melodies, often with the flat second for menace (`theory.scale(root, "phrygian")`).
A loop of two to four chords or a single melodic motif on piano, bells, plucks or strings, repeated with small changes.

## Drums (16 steps a bar; each drum channel is a sampler on MIDI 60)
- Kick: `x......x..x.....` as a start; move kicks around per section, they are the rhythm's personality.
- Clap or snare: `........x.......` (beat 3); a ghost clap just before it now and then.
- Hat: `x.x.x.x.x.x.x.x.` (eighths) as the bed.
- Rolls: short bursts of 32nds (`step_beats=0.125`) or sixteenth triplets (`step_beats=1/6`) in `theory.drums`, at the end of a phrase, not every bar.
- Open hat: one off-beat hit every bar or two.

## The 808
Long 808 notes on the 808 channel, starting with the kick and held to the next kick, mostly the root with octave jumps for movement.
Play in MIDI 24 to 48, one note at a time, tuned to the key.
The 808 is the bass and the sub; there is no separate bass part.

## Sounds to search
"hard trap kick", "crisp trap clap", "trap hi-hat closed", "dark piano melody", "music box bell", "orchestral string stab", "dark pluck"; choose by ear with `fl.guide("sounds")`.

## Bar map (about 2.5 minutes at 140 BPM)
- Intro 1-8: the melody alone, filtered or thin; a drum fill or riser into bar 9.
- Hook 9-24: full drums, 808, melody.
- Verse 25-40: lighter: fewer hats, a sparser 808, the melody's counterpart or no melody.
- Hook 41-56: everything back, plus a layer the first hook lacked.
- Bridge 57-64: drums out or half; the melody changes octave or instrument.
- Hook 65-80, Outro 81-84: the melody alone again.
An instrumental beat still follows the vocal shape: the hook is where a rapper's chorus would be.

## Mix
The kick and 808 share the sub: a short kick over a long 808, or no 808 note starting on the same step as a kick that rings (see `fl.guide("sidechain")` to duck it).
Hats bright and a little lower than the clap; pan rolls and percussion slightly off centre.
Melody in the mids, high-passed so it leaves the 808 alone.
A loud 808 drives the master's Maximus hardest; turn the 808 down before the mix sounds crushed.
Loudness target and the revise loop: `fl.guide("revise")`.

## Common mistakes
- Hat rolls in every bar: they stop meaning anything.
- An 808 out of key, or two 808 notes overlapping into a smear.
- The same 8 bars for the whole beat; the verse needs to drop something.
- The 808 so loud the kick disappears and the limiter pumps.

```python
import pyloudnorm as pyln
fl.set_tempo(140)
at = lambda notes, beat: [n.model_copy(update={"start": n.start + beat}) for n in notes]
d = lambda steps, v=0.85, step=0.25: theory.drums(steps, key=60, velocity=v, step_beats=step)
kick = [n for b in range(4) for n in at(d("x......x..x....."), b * 4)]
fl.track("Kick").write(kick, mode="replace")
fl.track("Clap").write([n for b in range(4) for n in at(d("........x......."), b * 4)], mode="replace")
hats = [n for b in range(3) for n in at(d("x.x.x.x.x.x.x.x.", 0.6), b * 4)] + at(d("x.x.x.x.x.x.", 0.6), 12)
roll = at(d("xxxx", 0.5, 0.125), 15) + at(d("xxx", 0.55, 1 / 6), 15.5)    # bar 4's last beat
fl.track("Hat").write(hats + roll, mode="replace")
starts = [n.start for n in kick] + [16]
roots = {0: 34, 1: 34, 2: 37, 3: 32}                        # Bb1 Bb1 Db2 Ab1
fl.track("808").write([Note(roots[int(s // 4)], s, e - s, 0.9) for s, e in zip(starts, starts[1:])], mode="replace")
r = fl.render("trap.wav", bars=4)
x, sr = fl.read_wav(r)
print(fl.meters(bars=4).summary, round(pyln.Meter(sr).integrated_loudness(x), 1), "LUFS")
```
