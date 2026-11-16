---
name: arrangement
description: How patterns, the playlist and automation work through fl.
---

How a song is laid out through `fl`, whatever shape you give it.

## Patterns and the playlist
In a new project, pattern 1 already starts the song at bar 1; rename it with `fl.rename_pattern`.
`fl.new_pattern` makes another pattern, or a copy of one to edit (`copy_of=`), and `fl.track(channel, pattern=number)` writes into it.
A pattern lasts as long as its notes, rounded up to whole bars, and its clips play that long: a note that ends just past a bar line adds a bar.
`fl.place` puts a pattern in the song; give it every bar the pattern plays at in one call (`help(fl.place)` says why).
`fl` cannot move or remove a clip once placed; FL's own Playlist window can.

## Automation
`fl.automate` draws a control's moves over bars, such as a channel's volume or an instrument parameter (find one with `fl.track(ch).params("cutoff")`).
It plays the automated bars in real time twice, and each `run` call has a time limit (`help(fl.automate)`).
A control holds the value its last move left it at.

## Timing and velocity
Notes from `theory.drums` and `Note` sit exactly on the grid at the velocity you give.
Swing or accents are arithmetic on each note's `start`, `length` and `velocity`, as the example does for the hats.

```python
fl.set_tempo(120)
kick = theory.drums("x...x...x...x..." * 4)                 # 4 bars, on MIDI 60
late = lambda pct: (pct - 50) / 200                          # beats
swing = lambda notes, pct=56: [n.model_copy(update={"start": n.start + late(pct), "length": n.length - late(pct)}) if round(n.start * 4) % 2 else n for n in notes]
hats = swing(theory.drums("xxxxxxxxxxxxxxxx" * 4, velocity=0.6))  # off-sixteenths a little late, still ending on the grid
chords = [Note(k, 0, 16, 0.6) for k in theory.chord("C4")]            # one chord held for 4 bars
a = fl.rename_pattern(1, "A")
b = fl.new_pattern("B")
fl.track("Pad", pattern=a.number).write(chords, mode="replace")
for ch, notes in (("Kick", kick), ("Hat", hats), ("Pad", chords)):
    fl.track(ch, pattern=b.number).write(notes, mode="replace")
fl.place(b, [5, 9])                                                  # every bar B plays at, in one call
cutoff = fl.track("Pad").params("cutoff")[0].name
take = fl.automate([{"position": 0, "value": 0.15}, {"position": 16, "value": 0.9}], bar=1, channel="Pad", param=cutoff)
print(take.pattern.name, [(p.number, p.name, p.beats) for p in fl.patterns])  # A and B: 16 beats each
```
