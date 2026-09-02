# FL Studio save and render API research

**Scope.** Issue [#161](https://github.com/origami-research/fl-studio-mcp/issues/161)
needs the live project written to its existing `.flp` and a WAV artifact for the
arena. This is documentation research only. FL Studio is not available in this
worktree, so every proposed invocation below is **[ASSUMED - needs live
verification]**, even where the public API name and signature are documented.

## Result

| Need | Best documented route | Confidence | Important limit |
| --- | --- | --- | --- |
| Save the current, already-named project | `transport.globalTransport(midi.FPT_Save, 1, midi.PME_System, midi.GT_All)` | High for the API identity; medium for the button value | An untitled project can legitimately open FL's save UI instead of writing a known `.flp`. |
| Render the current project or pattern to WAV from a MIDI controller script | **No documented call exists.** | High negative result | `ui.exportWav` is absent from both the Image-Line MIDI scripting reference and the current API-stub `ui` surface. |
| Render a saved project to WAV | Launch FL's documented CLI renderer on a saved copy: `OsxFL -R <project.flp>` on macOS, with the documented `/R`/`/Ewav` form on Windows. | High for the documented CLI capability; medium for this installation's executable spelling and flags | This is a process boundary, not a MIDI-scripting call, and renders the saved file rather than unsaved live state. |

The correct immediate conclusion is asymmetric: add a narrowly named save
operation to the controller, but do **not** replace `ui.exportWav` with another
invented `ui`, `general`, `transport`, or `arrangement` function. The supported
render design is `save -> confirmed file -> render saved copy with FL CLI`.

## Save candidate, ranked

### 1. `transport.globalTransport` + `midi.FPT_Save` [ASSUMED - needs live verification]

```python
transport.globalTransport(
    command: int,
    value: int,
    pmeflags: int = midi.PME_System,
    flags: int = midi.GT_All,
) -> int
```

`midi.FPT_Save` is constant `92` and is explicitly documented as **"Save
project."** The official MIDI scripting reference lists `FPT_Save` as a button
command and documents `globalTransport(command, value, pmeflags=PME_System,
flags=GT_ALL) -> int`; it also says scripts should pass an event's `pmeflags`
when called from an event callback. Our SysEx/OnIdle RPC has no originating
controller event, so `midi.PME_System` is the documented default and the least
speculative choice.

The stubs give the exact typed signature and the command's name. They leave
`value` undocumented. Use `1` as a press for this button command, but treat
that argument as the specific live-verification question, not as a confirmed
constant. The result is a handling flag, not a save receipt: `midi.GT_Cannot`
is `-1`, `midi.GT_Global` is `8`, and `midi.GT_Delayed` is `5`.

**Citations:** [Image-Line MIDI scripting reference: `globalTransport`](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/midi_scripting.htm#transport),
[Image-Line MIDI scripting reference: global transport commands](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/midi_scripting.htm#globaltransportcommands),
[stub signature](https://github.com/IL-Group/FL-Studio-API-Stubs/blob/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/transport/__init__.py#L97-L128),
[stub `FPT_Save` definition](https://github.com/IL-Group/FL-Studio-API-Stubs/blob/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/midi/__gt_commands.py#L278-L284),
[stub result flags](https://github.com/IL-Group/FL-Studio-API-Stubs/blob/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/midi/__gt_flags.py#L1-L39).

### Rejected save lookalikes

- `general.saveUndo(undoName: str, flags: int, update: bool = True) -> None`
  [ASSUMED - needs live verification]: documented as an undo-history point,
  not a project-file write. It cannot make live notes durable in the `.flp`.
  [Citation](https://github.com/IL-Group/FL-Studio-API-Stubs/blob/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/general/__undo.py#L6-L43).
- `general.getChangedFlag() -> int` [ASSUMED - needs live verification]: a
  useful observation before and after saving, but it only reports whether the
  project changed since the last save. It is not a save action.
  [Citation](https://github.com/IL-Group/FL-Studio-API-Stubs/blob/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/general/__fl_state.py#L64-L79).
- No `general.save`, `ui.save`, `arrangement.save`, or keyboard Ctrl+S API is
  documented. In particular, the current stubbed `ui` keyboard surface only
  exposes cut, copy, paste, insert, delete, enter, escape, yes, no, and arrow
  operations. [Citation](https://github.com/IL-Group/FL-Studio-API-Stubs/blob/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/ui/__keyboard.py).

## Render candidate, ranked

### 1. No MIDI-scripting WAV renderer [ASSUMED - needs live verification]

There is no function to call here. The official MIDI scripting reference's
full module/function inventory contains `transport.globalTransport` and the
`FPT_Save` command, but has no `exportWav`, `render`, or `export` entry. The
current API stubs agree: `ui`, `general`, `transport`, and `arrangement` export
no function whose name contains `render` or `export`; their source inventories
are linked below. Consequently, `ui.exportWav(path)` is not a candidate API
with a wrong signature. It is an unknown operation, matching the live-match
failure reported in #161.

**Citations:** [official MIDI API module inventory](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/midi_scripting.htm#scriptmodules),
[official transport and command inventory](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/midi_scripting.htm#transport),
[stub `ui` exports](https://github.com/IL-Group/FL-Studio-API-Stubs/tree/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/ui),
[stub `general` exports](https://github.com/IL-Group/FL-Studio-API-Stubs/tree/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/general),
[stub `transport` exports](https://github.com/IL-Group/FL-Studio-API-Stubs/blob/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/transport/__init__.py),
[stub `arrangement` exports](https://github.com/IL-Group/FL-Studio-API-Stubs/tree/9b4c7fa8fd2ac62af3478cdb2faa29f3f7c04e4c/src/midi_controller_scripting/arrangement).

### 2. FL command-line renderer [ASSUMED - needs live verification]

The supported non-interactive renderer is outside MIDI scripting. Image-Line's
audio-export documentation specifies `/R[filename]` to render a project to an
audio file, `/E` to select formats including `wav`, and `/O` for an output
directory. The repository has separately recorded a successful macOS form,
`OsxFL -R song.flp`, which writes the WAV beside the project.

```text
Windows documented shape: FL.exe /R[filename] /Ewav [project.flp]
macOS repository evidence: OsxFL -R saved-copy.flp
```

The CLI documentation describes a project renderer, not a pattern-only
renderer. A pattern-only WAV therefore has no documented MIDI scripting or CLI
call in this research. It would require a separate, explicit arrangement or
project-isolation design rather than silently rendering the wrong scope.

**Citations:** [Image-Line export and command-line documentation](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/fformats_save_export.htm),
[repository macOS render evidence](../21-v2-rewrite-spec.md#e4-macos-headless-render-works),
[existing capability verdict](../24-coverage-map.md#non-blocking-render-verdict-2026-08-31).

### Rejected renderer lookalikes

- `FHD_RenderProject` [ASSUMED - needs live verification] is a native plugin
  dispatcher command, not MIDI scripting. Existing repository SDK/probe
  evidence classifies it as a modal render dialog, so it is not an unattended
  WAV export replacement. [Citation](../25-fl-capability-catalog.md).
- Keyboard or menu automation is not an API-level renderer and cannot supply a
  destination path or completion receipt. Do not add it as a fallback.

## Concrete follow-up recommendation

When implementation is authorized, add a composite controller operation named
`project.save`, rather than exposing magic number `92` to the server:

```python
# [ASSUMED - needs live verification]
before = general.getChangedFlag()
handled = transport.globalTransport(
    midi.FPT_Save, 1, midi.PME_System, midi.GT_All
)
after = general.getChangedFlag()
return {"handled": handled, "changed_before": before, "changed_after": after}
```

It must import `midi`, reject `handled == midi.GT_Cannot`, and the server must
then confirm the known `.flp` was actually written by observing a changed mtime
or content hash and parsing the saved file. `changed_after == 0` is only a
secondary host-side signal: a save can fail, prompt, or target an untitled
project without producing the required artifact.

Do not add a controller `render_wav` operation. Replace the current
`render_wav()` implementation's `ui.exportWav` request with a saved-copy CLI
flow: call `project.save`, confirm the `.flp`, copy that confirmed file to an
owned render directory, invoke FL's CLI renderer on the copy, and validate the
WAV header and non-zero length before returning it. This preserves the live
project and makes the renderer's input an inspectable artifact. It also keeps
the blocking render out of `OnIdle` and out of the automatic ears loop.

## Required live verification

Use a disposable, already-saved project and record all four outcomes:

1. Call `project.save`; require a non-`GT_Cannot` result, project mtime/hash
   change, and the live notes present after reopening or parsing the `.flp`.
2. Call it again while clean; record the handling result and whether the mtime
   changes, rather than assuming either behavior.
3. Call it on an untitled project; verify it fails visibly or reports a modal,
   never falsely reports artifact persistence.
4. Render a saved copy with the local FL CLI; require a non-empty RIFF/WAVE
   file, and confirm the original live project remains unchanged.

Until those checks pass, `project.save` and the CLI invocation remain
**[ASSUMED - needs live verification]** and the arena must not label a match
artifact as persisted or listenable.
