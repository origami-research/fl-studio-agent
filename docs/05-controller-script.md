# 05 — The Controller Script (control plane)

`device_FLMCP.py` — a MIDI Controller Scripting device script running on FL's embedded
Python 3.9. It is a thin, dumb executor: decode SysEx → call the named API function →
encode the result. All intelligence lives in the MCP server.

## Environment constraints (design inputs)

- **Python 3.9**, embedded. No pip, no venv. Extra modules can be placed in FL's shared
  Python library folder [VERIFIED — manual], but v1 targets **zero dependencies
  beyond stdlib**, and treats even stdlib availability (`json`, `base64`) as
  [ASSUMED — Spike 3 verifies; both are pure-Python-representable if absent].
- **Cooperative scheduling**: `OnSysEx`/`OnIdle` run on FL's event loop. Slow handlers
  delay MIDI for the whole app. Budget: <1 ms typical, hard-decompose anything larger.
- **No note read/write** — by design this plane never touches note data (see 01).
- The script survives project switches (`OnProjectLoad`) — it re-announces `hello` so
  the server can invalidate its live-state cache.

## Structure

```
Settings/Hardware/FLMCP/
└── device_FLMCP.py     # single file, no imports beyond stdlib + FL modules
```

```python
# name=FLMCP Bridge Script
# receiveFrom=FLMCP Req

import transport, mixer, channels, patterns, playlist
import plugins, ui, general, arrangement, device
import json, base64   # availability validated in Spike 3

PROTO_VER = 1
MAGIC = (0x7D, 0x4D, 0x43)   # 0x7D + "MC"

def OnInit():
    _send_event({"op": "hello",
                 "script_ver": "0.1.0",
                 "fl_ver": ui.getVersion(),
                 "ppq": general.getRecPPQ()})   # [ASSUMED name — Spike 3]

def OnSysEx(event):
    frame = _decode(event.sysex)          # validate magic+version, reassemble chunks
    if frame is None: return
    result = _dispatch(frame)             # table lookup, execute, capture errors
    _send_response(result)                # chunked if >512B
```

### The dispatch table

The wire `op` is `<module>.<function>` (03). Dispatch is a **whitelist**, not
`getattr` on arbitrary strings:

```python
ALLOWED = {
    "transport": transport, "mixer": mixer, "channels": channels,
    "patterns": patterns, "playlist": playlist, "plugins": plugins,
    "ui": ui, "general": general, "arrangement": arrangement,
}

def _dispatch(frame):
    mod_name, _, fn_name = frame["op"].partition(".")
    if mod_name == "state":  return _composite(frame)       # snapshot ops
    if mod_name == "ping":   return {"id": frame["id"], "ok": True, "ret": "pong"}
    mod = ALLOWED.get(mod_name)
    fn = getattr(mod, fn_name, None) if mod else None
    if fn is None:
        return {"id": frame["id"], "ok": False, "err": f"unknown op {frame['op']}"}
    try:
        return {"id": frame["id"], "ok": True, "ret": fn(*frame.get("args", []))}
    except Exception as e:
        return {"id": frame["id"], "ok": False, "err": f"{type(e).__name__}: {e}"}
```

Passing raw FL API calls over the wire (rather than bespoke commands) means new
capability = server-side change only. The whitelist plus FL's own API surface is the
security boundary; there is no `eval`, no `exec`, no arbitrary code execution. (This is
where we deliberately diverge from Flapi, which is a general remote REPL. An MCP server
driven by a model should expose named functions, not an interpreter.)

### Composite ops (`state.*`)

Round trips are the expensive resource, so common read patterns get single-frame
composites executed script-side:

- `state.snapshot` → `{ppq, tempo*, playing, song_pos, current_pattern, pattern_count,
  channels: [{idx, name, muted, vol, pan, target_mixer_track}], mixer: [{idx, name,
  vol, pan, muted, solo}] }` (*tempo read via `mixer.getCurrentTempo()`
  [ASSUMED name — Spike 3]).
- `state.plugin_params(chan_or_track)` → `[{idx, name, value}]` — built from
  `plugins.getParamCount/getParamName/getParamValue`. Potentially thousands of params
  (VSTs) — paginated by the op's args and chunked in the response.

## Control-plane capability inventory (v1)

What the MCP server may call through this script, grouped by FL module — the concrete
subset we commit to wiring and testing, chosen against the API Stubs docs:

| Module | v1 functions |
|---|---|
| `transport` | `start`, `stop`, `record`, `isPlaying`, `getSongPos`, `setSongPos`, `setLoopMode`, `globalTransport` |
| `patterns` | `patternNumber`, `patternCount`, `jumpToPattern`, `getPatternName`, `setPatternName`, `getPatternLength` |
| `channels` | `channelCount`, `selectOneChannel`, `getChannelName`, `setChannelName`, `getChannelVolume/Pan` + setters, `muteChannel`, `getGridBit`, `setGridBit`, `getTargetFxTrack` |
| `mixer` | `trackCount`, `getTrackName/setTrackName`, `getTrackVolume/setTrackVolume`, `getTrackPan/setTrackPan`, `muteTrack`, `soloTrack`, `getRouteSendActive/setRouteTo`, `getCurrentTempo` |
| `plugins` | `isValid`, `getPluginName`, `getParamCount`, `getParamName`, `getParamValue`, `setParamValue`, `getPresetCount`, `nextPreset`, `prevPreset` |
| `general` | `getRecPPQ`, `undo`, `undoUp`, `saveUndo`, `getVersion`, `processRECEvent` (tempo set — see below) |
| `ui` | `setHintMsg`, `crDisplayRect`*, window show/hide for piano roll & mixer, `showWindow`, keyboard emulation for Ctrl+S (used only by `sync_from_project`) |
| `arrangement` | `jumpToMarker`, `getMarkerName`, `currentTime`, `selectionStart/End` |

(*Exact function names are normalized against the API Stubs during Spike 3; this table
fixes the *capability* commitment, not the final spelling.*)

### Known tricky operations

- **Set tempo**: no direct setter exists. The known technique is
  `general.processRECEvent(midi.REC_Tempo, bpm*1000, midi.REC_Control | midi.REC_UpdateControl)`
  [REPORTED — community scripts]. Validated in Spike 3; if unstable we fall back to
  `transport.globalTransport(FPT_TempoJog, …)` stepping.
- **Save project** (`sync_from_project` support): no `general.save()`. Route:
  `ui` keyboard emulation of Ctrl+S [ASSUMED — Spike 3; fallback: instruct user +
  detect mtime change on the project file].
- **Selecting mixer track for `plugins.*`**: plugin addressing is `(index, slotIndex)`
  where index is channel # or mixer track # — the server tracks which addressing form
  each target needs (channel rack instrument vs mixer effect).

## Registration & handshake UX

FL requires the user to (once): enable the script under MIDI settings — set `FLMCP Req`
input to controller type "FLMCP Bridge Script" with a port number, and add `FLMCP Res`
as output with the matching port. The installer documents this with screenshots; the
`hello` event doubles as the "it worked" signal, which the MCP server surfaces as a
green check in its startup diagnostics (`doctor` tool, 06).
