---
name: arrangement
description: Turn a loop into a song with sections, builds and drops.
---

A loop that plays unchanged for minutes sounds flat and loopy, however good the loop is.
An arrangement is a plan of which parts play in which bars, so the energy rises and falls on purpose.

## Plan the bar map first
Write the map before any notes: section names, start bars, and which parts play in each.
Clips cannot be moved or removed once placed, so a plan made first is cheaper than a fix later.
Make sections whole phrases of 4, 8, 16 or 32 bars; listeners expect a change at those boundaries (a convention of most Western pop and dance music).
Each genre guide gives a map to start from; a brief's own bar numbers always win.

## One pattern per section
Pattern 1 already starts the song at bar 1: rename it to your first section with `fl.rename_pattern`.
Make each other section with `fl.new_pattern` and write it with `fl.track(channel, pattern=number)`.
Write a section's notes for its full length; a pattern lasts as long as its notes.
Give `fl.place` every bar a pattern plays at in one call (`help(fl.place)` says why).
To play a section again with changes, copy it with `fl.new_pattern(name, copy_of=...)` and edit the copy.

## Build energy, in order of strength
1. Add and remove layers. The kick and bass leaving and returning is the biggest change you can make; a breakdown without them makes the next drop land.
2. Density: hats from eighths to sixteenths, a snare roll that doubles its rate in the last bar or two of a build.
3. Filter: open a low-pass on a pad, pluck or the whole drum part across a build with `fl.automate` on the instrument's cutoff (find it with `fl.track(ch).params("cutoff")`).
4. Risers and impacts: Sampler 1 starts as a riser and Sampler 2 as a crash; put the riser in the last bars of a build and the crash on the downbeat of the drop.
5. A gap: silence or the kick dropping out for the last beat or bar before a drop makes the drop hit harder.
Bring things back on the section's first beat, not a beat late.

## Keep repeats alive
Change something every 4 or 8 bars, even inside a section: a fill in the last bar, a hat pattern variation, a chord inversion, a layer in or out.
Swing and velocity stop programmed drums sounding robotic.
Swing delays every second note of the grid it is set on, sixteenths in house and often eighths in hip hop and lo-fi.
In MPC terms 50% is straight and 66% is a triplet feel, and most grooves sit between (tune by ear).
A swing of p percent moves each off-sixteenth by (p - 50) / 200 beats, or each off-eighth by (p - 50) / 100 beats.
Accent the beats and soften the in-between hits, for example velocity 0.9 on beats and 0.6 to 0.75 elsewhere.

## Automation costs time
`fl.automate` plays the automated bars in real time twice, and a run has a time budget (`help(fl.automate)`).
Automate only the bars that move, and split long arrangements across runs.
A control holds the value its last move left it at, so start every sweep at the value the section should open with.

## Check it
Render the whole song once the map is placed and measure loudness per section (see `fl.guide("listening")`).
Sections should differ clearly; if the loudness per section is nearly the same from start to end, the arrangement is not doing its job.
Our showcase scoring treats less than about 4 LU between the quiet and loud passages as flat; that is our heuristic, not an industry standard.

```python
import pyloudnorm as pyln
fl.set_tempo(124)
at = lambda notes, beat: [n.model_copy(update={"start": n.start + beat}) for n in notes]
kick = theory.drums("x...x...x...x..." * 8, key=60)                 # 8 bars, a sampler plays MIDI 60
hats = theory.drums("..x...x...x...x." * 8, key=60, velocity=0.6)
chords = [Note(k, bar * 4, 4, 0.6) for bar in range(8) for k in theory.chord(("A3", "F3", "C4", "G3")[bar % 4], "minor" if bar % 4 == 0 else "major")]
intro = fl.rename_pattern(1, "Intro")
build, drop = fl.new_pattern("Build"), fl.new_pattern("Drop")
for p in (intro, build):
    fl.track("Pad", pattern=p.number).write(chords, mode="replace")
fl.track("Hat", pattern=build.number).write(hats, mode="replace")
fl.track("Snare", pattern=build.number).write(at(theory.drums("x.x.x.x.xxxxxxxx", key=60), 28), mode="replace")
fl.track("Sampler 1", pattern=build.number).write([Note(60, 24, 8)], mode="replace")  # its riser, last 2 bars
for ch, notes in (("Kick", kick), ("Hat", hats), ("Pad", chords), ("Crash", [Note(60, 0, 4)])):
    fl.track(ch, pattern=drop.number).write(notes, mode="replace")
fl.place(build, 9)
fl.place(drop, [17, 25])
cutoff = fl.track("Pad").params("cutoff")[0].name
fl.automate([{"position": 0, "value": 0.15}, {"position": 32, "value": 0.9}], bar=9, channel="Pad", param=cutoff)
song = fl.render("build.wav", start_bar=9, bars=12)     # the build into the drop
x, sr = fl.read_wav(song)
bar, meter = int(sr * 240 / song.span.tempo_bpm), pyln.Meter(sr)
print("build", round(meter.integrated_loudness(x[:8 * bar]), 1), "drop", round(meter.integrated_loudness(x[8 * bar:]), 1), "LUFS")
```
