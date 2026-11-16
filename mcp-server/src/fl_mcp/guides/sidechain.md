---
name: sidechain
description: Ducking one part under another, such as the kick, with drawn automation.
---

Code cannot key a compressor from another track (`help(fl.effect)` says why).
To make a part duck when the kick hits, draw the ducking instead, with one `fl.automate` on the part's mixer track volume.

Facts that shape the curve:

- FL plays recorded moves about 0.1 s late, so a dip meant for a beat starts that much early.
- A very fast return to full level can click.
- How deep, how long, and which parts duck are yours to choose.

A short `fl.render` shows whether the level drops where you meant (see `fl.guide("listening")`).

```python
pad = next(c for c in fl.channels if c.name == "Pad")
top = next(m.volume for m in fl.mixer if m.index == pad.mixer_track)
early = 0.1 * fl.tempo / 60  # FL's lag, in beats
low = 0.25  # the dip's fader value
dip = [{"position": 0, "value": low}, {"position": 0.35 - early, "value": top}]
for beat in range(1, 64):  # one dip per beat, 16 bars
    dip += [{"position": beat - early - 0.02, "value": top},
            {"position": beat - early, "value": low},
            {"position": beat - early + 0.35, "value": top}]
take = fl.automate(dip, bar=1, mixer_track=pad.mixer_track)
print(take.pattern.name, "max error", take.max_error)
```
