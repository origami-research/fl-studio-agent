# 10 - v0 Vertical Slice (implemented)

The first working implementation: **ask Claude for notes → they appear in FL Studio's
piano roll.** This is milestone M1 from [08-roadmap.md](08-roadmap.md), built with one
deliberate substitution that lets it ship before Spike 1 completes.

**Status: working and verified in FL Studio 2025 on macOS.** See the smoke test below.

## The substitution

The target architecture's note plane is a **native C++ Bridge plugin** calling
`FHD_AddNotesToPR` ([04](04-native-plugin.md)). That mechanism is still
**[REPORTED], not [VERIFIED]** - it's the single unproven assumption the whole design
rests on, and it needs a C++ toolchain, the FL SDK, and a binary install.

v0 substitutes a **piano roll `.pyscript`** as the note-plane executor. The
`flpianoroll` API's ability to write notes is **[VERIFIED]** - it's documented,
shipped with FL, and used by FL's own built-in scripts. So v0 has *zero* unverified
assumptions and works today.

```
                        v0 (now)                     target (docs/02)
note plane executor     piano roll .pyscript         native C++ effect plugin
trigger                 user runs it from a menu     automatic (Idle poll)
targeting               the open piano roll only     any channel, any pattern
control plane           none                         SysEx ⇄ controller script
verification status     works, tested                Spike 1 pending
```

Everything upstream of the executor - MCP server, tool surface, note model, spool
protocol - is written to the target design and carries over unchanged.

## What works

| | |
|---|---|
| `write_notes` | Queue notes; `merge` or `replace` mode |
| `clear_notes` | Queue a clear of the open piano roll |
| `fl_status` | Is the script installed? Is a command pending? Did the last one land? |

Notes accept musical input: `{"key": "C4", "start": 0, "length": 1}` - note names or
MIDI numbers, positions and durations in **beats**, velocity/pan as 0..1.

## Limitations (all removed by the Bridge plugin)

1. **Manual trigger.** The MCP server can only *queue* an edit. FL applies it when the
   user runs Tools > Scripting > "MCP Write Notes". Tool responses say this explicitly
   (`status: "queued"`, `next_step: ...`) so the model doesn't claim success the user
   hasn't seen.
2. **No targeting.** A piano roll script can only edit the score it was invoked on, so
   the notes land wherever the user has the piano roll open. No `pattern`/`channel`
   arguments exist yet - adding them would be a lie.
3. **No control plane.** No transport, mixer, or plugin parameters. Those need the
   Python controller script ([05](05-controller-script.md)), not built yet.
4. **No note read-back.** `flpianoroll` *can* read the score, but v0 doesn't surface it;
   the shadow score from [06](06-mcp-server.md) isn't implemented.

## Deviations from the documented protocols

Two intentional simplifications, to be reconciled when the Bridge plugin lands:

**Spool layout.** [03](03-protocols.md) specifies sequenced `commands/`/`responses/`
directories for a continuously-polling consumer. v0's consumer is user-triggered and
one-shot, so exactly one command is ever pending and a single `pending.json` +
`result.json` pair is enough. The *command payload* already matches the documented
shape, so only the file layout changes.

**Beats on the wire, not ticks.** [03](03-protocols.md) has the spool carry PPQ ticks,
with the server converting using a PPQ learned from the control-plane handshake. v0 has
no control plane - but the executor knows `flp.score.PPQ` at runtime, so the spool
carries beats and conversion happens inside FL.

**This deviation is worth keeping.** It puts unit conversion where the ground truth
lives, survives per-project PPQ changes with no re-handshake, and keeps the server free
of FL-specific units. Proposed amendment to 03 when the Bridge plugin is written: the
plugin reads PPQ from the host and the spool stays in beats.

## Layout

```
mcp-server/
  src/fl_mcp/
    server.py    MCP tools; the only file that talks to the model
    notes.py     note names, validation, defaults - no FL-specific units
    spool.py     spool paths + atomic read/write
    install.py   copies the .pyscript into FL's settings folder
  tests/
    fake_flpianoroll.py   stand-in matching the official API stubs
    test_pyscript.py      runs the real .pyscript against it, end to end
    test_notes.py         note parsing and validation
fl-scripts/piano-roll/
  MCP Write Notes.pyscript    the in-FL executor
```

## FL Studio's embedded Python sandbox [VERIFIED - the hard way]

Every one of these was learned by watching a real script fail. They are not
documented by Image-Line anywhere we could find, and they invalidate the obvious
way to write this script.

| Behavior | Consequence for our design |
|---|---|
| `open()` succeeds **only inside FL's own data tree**. Elsewhere: `SystemError: <class '_io.FileIO'> returned NULL without setting an exception` | The spool must live under `Image-Line/FL Studio/Settings`, not in a conventional app-data location |
| **Write permission is narrower than read.** `Settings/Piano roll scripts/**` accepts writes; `Settings/FLMCP/` is readable but read-only | The spool lives under `Piano roll scripts/`. Consumption must never depend on the folder a command was *read* from |
| `os.path.exists()` works **anywhere** - including paths `open()` will refuse | A visible-but-unreadable file is a real state; "not found" and "cannot open" must be distinguished or the error misleads |
| `open()` rejects the `encoding=` keyword | Plain `open(p, "r")` / `open(p, "w")` only |
| `os.remove()` is **blocked outright**: `SystemError: <built-in function remove> returned NULL...` | Commands are consumed by overwriting with a tombstone, never deleted. `os.replace`/`os.rename` are assumed blocked too and avoided |
| Blocked calls raise **`SystemError`**, not `OSError` | `except OSError` silently fails to catch FL's refusals. Every guard catches `Exception` |
| FL calls `apply()` with **no arguments** when a script defines no `createDialog()` | `def apply(form)` raises TypeError; the signature must be `apply(form=None)` |
| Top-level module code **does** execute, before `apply()` | Doing the work at module level means no dialog to dismiss - but `apply()` still fires afterward, so a re-entry guard is required |

## Verification status

**Automated (45 tests passing).** `tests/fl_env.py` emulates FL's sandbox - restricted
`open`, blocked `os.remove`, `SystemError` failure mode, bare `apply()` call, top-level
execution - and every `.pyscript` test runs the real script file through it.

The first version of these tests emulated the flpianoroll *API* but not the *sandbox*,
and passed while the real script failed three times in a row. That is the whole reason
`fl_env.py` exists.

**The emulator is itself verified by mutation testing.** Reverting the script to each
historical bug (`os.remove` instead of tombstone, `except OSError`, `apply(form)`,
`encoding=`, spool outside FL's tree) must make the suite fail. All five are caught; an
emulator that doesn't reproduce known failures is worthless, so this check should be
re-run whenever `fl_env.py` changes.

The MCP protocol layer was verified separately with a real stdio client: tools list,
schemas generate, calls round-trip, errors are structured.

**Not automated: FL Studio itself.** `flpianoroll` exists only inside FL, and running a
piano roll script requires the piano roll GUI - there is no headless or CLI invocation.
GUI automation is not a viable substitute either: FL draws its own menus, so the piano
roll menu is not exposed through macOS accessibility. So no test here proves
`flp.score.addNote` actually writes notes. That is what the manual smoke test is for,
and it is the only remaining gap.

## Manual smoke test - PASSED (FL Studio 2025, macOS)

1. `cd mcp-server && python -m fl_mcp.install`
2. Restart FL Studio; open a project with any instrument channel.
3. Register the server with your MCP client (see [../mcp-server/README.md](../mcp-server/README.md)).
4. Ask Claude for notes.
5. Double-click the channel to open its piano roll.
6. Run the piano roll's **▶ menu > Tools > Scripting > MCP Write Notes**.
   (Not FL's global Tools menu - a different menu with the same name.)

**Result: verified end to end.** A 7-note A-minor figure was queued and landed exactly
as specified - every pitch, every start position, and the deliberately longer final
note rendering at 2× the width of the others. `mode="replace"` cleared the previous
contents first. The script's own report:

```json
{"ok": true, "summary": "added 7 note(s) at PPQ 96", "note_count": 7, "consumed": true,
 "writable_roots": [".../Piano roll scripts", ".../Piano roll scripts/FLMCP"]}
```

Now [VERIFIED] as a result:

- `flp.score.addNote` writes notes to the live piano roll
- top-level execution runs with **no dialog**, and `apply()` is then called bare
- beats → ticks conversion is correct at PPQ 96, for both position and length
- `clearNotes(True)` implements `replace`
- **the octave convention**: our `A3` (MIDI 57) displays as `A4` in FL - FL labels
  notes one octave higher than scientific pitch notation, exactly as documented

Still [ASSUMED]: whether one write equals one undo step (untested).

## Next

Spike 1 ([08](08-roadmap.md)) - build the minimal C++ effect and confirm
`FHD_AddNotesToPR` writes cross-channel. If it does, the Bridge plugin replaces the
pyscript as the spool consumer, the manual trigger disappears, and `write_notes` gains
its `pattern` and `channel` arguments. Nothing above the spool changes.
