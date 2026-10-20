# 11 - Interface & Representation Spec (plain language)

This document records everything we decided in the design workshop and how the
system will actually work.

## The big picture in one paragraph

A model (like Claude) talks to our server.
Our server talks to FL Studio through two small programs that live inside FL.
The model always speaks in musical terms - "put these notes on Bass, bar 2" -
and the server handles all translation into FL's internal machinery.
Because FL never tells outside software what notes are in the project, the
server also keeps its own record of everything it did, so the model can
remember its work.

## What we decided, listed plainly

| Topic | Decision |
|---|---|
| How the model sends notes | Plain JSON tool calls for simple things; real Python in a notebook-style session for everything else. No in-between shorthand dialect (cut - the kernel makes it redundant) |
| Types inside the server | One set of Python classes (pydantic) defines every data shape; tool schemas and file parsing are generated from them |
| Where we save things | Ordinary files in a folder per project on the user's computer; no background service |
| Remembering notes we wrote | The server writes down every note it sends (the "shadow score") because FL can't be asked |
| Naming notes | We invent our own IDs (fingerprints of the note data) since FL has none |
| Undo | Every action the model takes shows up as one named undo step in FL, and we also keep our own log file |
| Running Python for the model | A long-lived Python kernel like a Jupyter notebook; full Python, confined to its workspace by the OS, not by Python filtering ([38](38-python-kernel.md)) |
| Doing multiple things at once | Skipped for now - things just run in order |
| Reading notes back | Try asking FL nicely first (probing its message bus); fall back to a piano-roll script; memory-hacking only as a last resort |

## The layers

```
The model (Claude)
    |  speaks: music - beats, note names, instrument names
    v
Our MCP server
    |  translates music into FL's units, keeps records
    v
Two helpers inside FL Studio
    controller script  - does play/stop, mixer, knobs (via virtual MIDI cable)
    bridge plugin      - writes notes into the piano roll (via JSON files in a folder)
    v
FL Studio itself
```

JSON only appears where data crosses between programs (model to server, server
to FL helpers, server to disk). Inside the server everything is real typed
Python objects.

## The data shapes

Every note the model sends looks like this:

    {"key": "C4", "start": 0.0, "length": 1.0, "velocity": 0.8}

- key - note name like "C4" or a MIDI number like 60 (same thing)
- start - position in beats from the start of the pattern
- length - duration in beats
- velocity / pan - 0 to 1, with sensible defaults

The server checks these against a Python class and converts them into whatever
FL needs (ticks, 0-128 ranges, and so on).
The model never deals with FL's units.

A group of notes gets an ID, which is a fingerprint (hash) of its contents.
If the model asks "what was that bassline from earlier?", the ID is how we
find it.

## Representation invariant

Pydantic models are the single source of truth for FL state, edits, and
commands. JSON appears only at boundaries: MCP input/output, command transport,
and durable files. Every other shape is a projection of those typed models.

That gives the system four rules:

- Resting FL values are model defaults, so compact model-facing views can use
  `exclude_defaults=True` without a custom compression format.
- Durable commands and logs are dumped in full. An explicit zero must not be
  confused with an omitted field.
- Note and note-set identities are content hashes of canonical typed values,
  salted with the schema version so stale caches invalidate on schema changes.
- User edits and model edits are both represented as structured deltas. A delta
  records what changed, not a replay of the action that probably caused it.

## How the model remembers its work (the "shadow score")

FL Studio offers no way to ask "what notes are in the piano roll right now?".
None.
So when the model writes a bassline, the server writes down its own copy.
That copy is the shadow score.

It can go out of date: if the user drags notes around by hand, the server's
copy still shows the old version and nothing warns it.
So every answer about notes says where it came from:

- "from my records" (might be stale)
- "just read from the saved file" (fresh)

and the model can request a fresh read (save the project, parse the file) when
it needs certainty.

## Naming things (references)

FL identifies containers by number: channel 4, pattern 3, mixer track 7.
Those numbers work and stay valid while the project is open.
The model can use names ("Bass") and the server looks up the number.

Notes have no ID anywhere - not even inside FL; a note is just its position
and pitch.
So we make our own: the fingerprint described above.

When the user edits notes by hand, we re-match them after the next fresh read:

1. Notes that are byte-for-byte the same match instantly.
2. Notes that moved a little or changed pitch a little match their old
   neighbor (nearest position+pitch, like git matching lines across edits).
3. Leftovers are recorded as "added" or "removed".
4. Each matched group gets a confidence score; low confidence is shown to the
   model rather than hidden.

So "the bassline" stays the same object to the model even after the user has
been dragging notes around - usually.
When matching is unsure, the model is told.

## History: undo and the action log

Two records, both kept:

1. In FL itself: every model action becomes one named undo step
   ("mcp: write bass"), so the user sees the model's work in FL's history
   panel and can Ctrl+Z it.
2. In our folder: `actions.jsonl`, one line per action, oldest first.
   Each line records what was done and a fingerprint of the notes before and
   after.
   Read the file bottom-up to answer "what actions built this?".
   Together with the starting snapshot, replaying the log rebuilds the whole
   project.

## Starting from an existing project (bootstrap)

1. The server asks FL for the layout: channels, patterns, mixer tracks, names.
2. It saves a copy of the project and reads the notes out of the saved file,
   so its records start from ground truth rather than empty.
3. The action log opens with a "bootstrap" entry.
4. From then on, the model can augment what is already there: reference by
   name or number, write through the normal paths.

## Reading notes back (the ladder)

Nothing in FL's public API reads notes today.
We try three things in order of preference:

1. **Probe FL's message bus.** The plugin-host interface is a numbered list of
   messages; only about 60 are documented.
   We send each undocumented number and watch what happens, looking for one
   that returns note data.
   Safe (worst case: an error on a scratch project), and if one exists, the
   whole "shadow score" machinery shrinks to a simple cache.
2. **Piano-roll script dump.** FL's piano-roll scripting can read the open
   score; a small script can dump it to a file for us.
   Limited (only the score that is open, needs a trigger) but officially
   supported.
3. **Read FL's memory directly.** Works, but breaks on every FL update and a
   bug can corrupt the project.
   Last resort only.

Note-level *selection* (which notes the user has highlighted) is in the same
boat: the probe hunt explicitly looks for it; the piano-roll script can
partially read it.

## The Python notebook session

When simple tool calls are not enough (iterate over notes, transform them,
edit several tracks in one go), the model writes a short Python program and
runs it in a persistent session - like a Jupyter notebook that stays alive
between turns.

- Full Python in its own process: every import, files, `%pip install`.
  The operating system confines it to its workspace; on Windows boxes it is an
  AppContainer ([38](38-python-kernel.md)).
- No separate scratch memory: the model's conversation history is its memory.
- Not built: the planned *handle plus a summary* for big note collections and
  stale-handle refusal inside the kernel. Large collections are summarized by
  `get_notes` instead, and a cell's result is the plain repr of its value.

## Storage layout

    ~/Library/Application Support/FLMCP/
    +-- spool/<session>/...        temporary command files, cleaned up
    +-- projects/<project-key>/
        +-- shadow.json            the server's record of notes
        +-- actions.jsonl          the action log
        +-- manifest.json          channel/pattern names and numbers
        +-- params/<plugin>.json   cached parameter names per plugin

Plain files, no background service.
If a second program ever needs the same data, upgrade to one SQLite file in
the same folder - still no service.

## Order of work

| Stage | What |
|---|---|
| Spike 1.5 | Probe FL's message bus for a note-reading message (before or alongside Spike 1) |
| Spike 1 | Prove the note-writing path works cross-channel |
| Spike 2/3 | Command folder loop; control script echo + undo introspection |
| M1 | First demo (model writes a beat) + the Python classes replace today's hand-rolled checks |
| M2 | Full tool set + fingerprint-based references |
| M3 | Notebook session + bootstrap for existing projects |
| M4 | Fresh-read tool + note re-matching + render + log replay |
| M5 | Packaging and release |

## Known problems and how we handle them

- **Spike 1 is still the gamble.** If cross-channel writing fails, the note
  delivery design changes before anything else matters.
- **The probe may find nothing.** Then the piano-roll dump is the fallback and
  the shadow score stays as designed.
- **Re-matching notes is guesswork at the edges.** Confidence scores and
  telling the model when a reference degraded keep this honest.
- **Undo introspection may be too limited** to tell whose edit is on top; the
  drift warning then falls back to convention (verify in Spike 3).
- **The notebook is trusted, not jailed.** Accepted for now; if it ever
  matters, restrict the process it runs in, not the language.
- **Fingerprint stability across format changes** needs a version tag stored
  with every saved file from day one.

## Surface outline

Everything the system exposes, grouped by who touches it.

### A. Tool surface (model calls these over MCP)

Session and health:

- `fl_doctor` - is everything connected? what exactly is broken and how to fix it
- `get_session_state` - tempo, playing, current pattern, channels, mixer tracks
- `sync_from_project` - save + fresh read; rebuilds the note records

Notes:

- `write_notes(pattern, channel, notes, mode)` - put notes in the piano roll
- `clear_notes(pattern, channel)`
- `get_notes(pattern, channel?, filter?)` - read from records or fresh read, labeled
- `preview_notes(channel, notes)` - hear them without writing

Composition helpers (pure math, no FL round trip):

- `chord_to_notes`, `quantize_notes`, `humanize_notes`

Transport and arrangement:

- `play`, `stop`, `record`, `set_tempo`, `set_song_position`,
  `select_pattern`, `set_pattern_name`, `set_loop_mode`, `undo_last`

Channels and step sequencer:

- `list_channels`, `select_channel`, `set_channel_volume/pan`,
  `mute_channel`, `set_step_grid`, `load_sample`

Mixer:

- `list_mixer_tracks`, `set_track_volume/pan`, `mute_track`, `solo_track`,
  `name_track`, `route_track`

Sound design:

- `list_plugin_params`, `get_plugin_param`, `set_plugin_param`, `set_plugin_preset`

Render:

- `render_project` - bounce a saved copy to wav/mp3/flac

Deliberately absent: load plugin, create pattern, add channel (impossible live;
the template project covers them at setup time).

### B. Kernel surface (model writes Python in the notebook session)

    fl.track("Bass")            -> handle (name resolved to number once)
    bass.notes(filter?)         -> lazy handle + summary, materialize on demand
    bass.write(notes, mode)     -> queued command, applied on cell end
    fl.play() / fl.stop() / fl.set_tempo(140)
    fl.undo_last()
    helpers: chord(), scale(), drums("x---x---"), transpose(), quantize()

Rules: reads come from the server's records (labeled fresh or stale);
writes flush when the cell finishes; stale project handles refuse to run.

### C. Reference surface (how anything is addressed)

- Instruments, patterns, mixer tracks: by name (looked up) or FL number
- Note groups: by fingerprint (hash of content), with confidence after user edits
- Selection: coarse (which channel/pattern/mixer track is selected) is readable;
  note-level selection only via the readback ladder

### D. Readback surface (how truth is established)

- Labeled reads: "from my records" vs "just read from the saved file"
- `sync_from_project` for certainty
- Probe / piano-roll dump / memory read, in that order, per the ladder section

### E. Human-facing surfaces (the user sees these)

- FL's own history panel showing named "mcp:" undo steps
- `actions.jsonl` - the full construction history, readable in any editor
- The template project - pre-provisioned channels/patterns
- `fl_doctor` output - plain fix-it instructions

### F. FL surfaces underneath (recap; full detail in 01-fl-studio-surfaces.md)

- MIDI controller script - transport, mixer, params, selection, undo
- Bridge effect plugin - note writing, sample loading, note preview
- Piano-roll scripting - read-only dumps (ladder step 2)
- Command-line render - offline bounce
- Saved .flp files via PyFLP - ground truth reads

## Freshness policy (best-of-all-worlds)

The rule "trust records, earn freshness" becomes a layered policy.
Each layer removes one source of doubt, in order of cost.

### Layer 1 - live structure signals (free, always on)

FL's controller script already receives change callbacks: OnDirtyChannel,
OnDirtyMixerTrack, OnRefresh, OnProjectLoad.
The server treats these as free notifications that the layout (channels,
patterns, mixer, plugin slots) changed, and invalidates cached numbers and
the manifest the moment they arrive.
No polling, no saves.
Note edits do NOT fire these events - they only cover structure.

### Layer 2 - records for model work (free, exact)

Everything the model wrote this session is exact in the server's records.
Reads, filtering, transforms: instant, no verification needed.
The only adversary is human hand-editing of notes.

### Layer 3 - targeted fresh reads (the workhorse)

Instead of syncing the whole project, sync one channel's pattern:
save (unavoidable - FL writes notes only on save), copy, parse just that part.
The model requests this when it is about to act on a part it did not write,
or after the user says they edited by hand.
Cheap because the parse is small and scoped.

### Layer 4 - autosave parsing (free background refresh)

FL already writes backup/autosave files on a timer.
The server parses those in the background for soft freshness - no
user-visible saves, no undo-history spam.
Lags by the autosave interval, so it is a refresher, never the authority for
a critical action.

### Layer 5 - the probe / dump (futures that collapse the policy)

If the dispatcher probe finds a note-getter, reads become live and free and
this whole policy shrinks to "just ask FL".
The piano-roll dump script is the partial version: near-live truth for the
open score, which is exactly where hand-edits happen - pursued only if it
can be triggered without keystroke hacks.

### Decision rule the model follows

- Reading model-written content: records, always.
- Structure questions: live getters, kept honest by Layer 1 signals.
- About to modify a part the model did not write: Layer 3 targeted read first.
- High-stakes actions (render, destructive replace): targeted read first.
- Background: Layer 4 keeps everything softly current between interactions.

### Addendum: control-plane transport decision

MIDI SysEx stays as the control-plane transport for now (it is the proven,
Flapi-shipped path into the embedded interpreter).
A file-spool control plane (one transport for everything, no MIDI port setup,
less code) is filed as an open issue to evaluate after the filesystem-access
spike on the embedded interpreter - it would delete the SysEx framing,
encoding, chunking, and port-setup layers entirely.
Latency is not the deciding factor: tens of milliseconds is invisible at
composition scale.
