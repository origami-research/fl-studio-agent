---
name: sidechain
description: Make a pad or bass pump with the kick.
---

Pumping is a part ducking each time the kick hits, so the kick cuts through and the groove breathes.
Code cannot key a compressor from the kick (FL sets a sidechain input only in the plugin's window), so draw the ducking instead: one `fl.automate` on the part's mixer track volume, a dip on every kick.

How the dip is shaped, and why:

- Duck to 0.25 on the fader (about 14 dB down) and come back over about 0.35 beats; a shorter release clicks, a longer one smears the groove.
- FL plays recorded moves about 0.1 s late, so start each dip that much early.
- Pump the parts that fight the kick for the low end or the space (pad, bass, chords), not the drums.

Check it with `fl.render` of a few bars: the part's level should drop on every beat (see `fl.guide("listening")`).

```python
pad = next(c for c in fl.channels if c.name == "Pad")
top = next(m.volume for m in fl.mixer if m.index == pad.mixer_track)
early = 0.1 * fl.tempo / 60  # FL's lag, in beats
low = 0.25  # about 14 dB down
dip = [{"position": 0, "value": low}, {"position": 0.35 - early, "value": top}]
for beat in range(1, 64):  # one dip per beat, 16 bars
    dip += [{"position": beat - early - 0.02, "value": top},
            {"position": beat - early, "value": low},
            {"position": beat - early + 0.35, "value": top}]
take = fl.automate(dip, bar=1, mixer_track=pad.mixer_track)
print(take.pattern.name, "max error", take.max_error)
```
