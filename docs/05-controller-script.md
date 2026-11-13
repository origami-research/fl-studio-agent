# 05 - The Controller Script (control plane)

`device_FLMCP.py` - a MIDI Controller Scripting device script running on FL's embedded
Python 3.9. It is a thin, dumb executor: decode SysEx → call the named API function →
encode the result. All intelligence lives in the MCP server.

## Environment constraints (design inputs)

- **Python 3.9**, embedded. No pip, no venv. Extra modules can be placed in FL's shared
  Python library folder [VERIFIED - manual], but v1 targets **zero dependencies
  beyond stdlib**, and treats even stdlib availability (`json`, `base64`) as
  [ASSUMED - Spike 3 verifies; both are pure-Python-representable if absent].
- **Threads**: FL calls `OnSysEx` on its MIDI input thread and `OnIdle` (about every 20 ms) on
  its main thread. So `OnSysEx` only queues the request it reassembled, and `OnIdle` runs every
  queued request and sends every response.
  A response sent from `OnSysEx` (`device.midiOutSysex`) waited for the main thread while the
  main thread waited on the MIDI thread, and FL never answered again while its window stayed
  responsive (py-spy on a frozen FL, #328).
  It struck automation takes, renders and meters, more often the more frames a request or
  response took; the traffic cuts of #311, #318 and #325 made it rarer, not impossible.
  A request is answered within one idle tick, and not while FL shows a modal dialog (FL does
  not call `OnIdle` then).
  Each request says how long the engine waits for it (`wait`, seconds); one still queued after
  that is dropped, so a change the engine already reported as failed cannot land late, or
  twice when the engine retries.
  Slow handlers delay the whole app. Budget: <1 ms typical, hard-decompose anything larger.
- **No note read/write** - by design this plane never touches note data (see 01).
- The script survives project switches (`OnProjectLoad`) - it re-announces `hello` so
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
                 "ppq": general.getRecPPQ()})   # [ASSUMED name - Spike 3]

def OnSysEx(event):                      # FL's MIDI input thread: queue only
    frame = _decode(event.sysex)          # validate magic+version, reassemble chunks
    if frame is not None:
        _inbox.append((frame, time.time()))

def OnIdle():                             # FL's main thread: run and answer
    while _inbox:
        frame, arrived = _inbox.pop(0)
        if time.time() - arrived > frame.get("wait", inf):
            continue                      # the engine stopped waiting: never run it late
        result = _dispatch(frame)         # table lookup, execute, capture errors
        if result is not None:            # None: deferred, answered when it lands
            _send_response(result)        # chunked if >512B
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

### Channel indices are global

Every channel index on the wire is FL's global index, the one the song file uses.
FL's channel functions otherwise count only the channels in the channel rack's active filter group, so with a group showing, channel 0 can be any channel and a write lands on the wrong one.
The composites pass FL's `useGlobalIndex` flag themselves (`channelCount(1)`, `getChannelName(i, 1)`, ...), and the server's `ControlLink.request` appends it to every direct `channels.*` and `plugins.*` call (`GLOBAL_INDEX_FLAG_AT` in `session/link.py`), whose caller passes every argument before it.

### Composite ops (`state.*`)

Round trips are the expensive resource, so common read patterns get single-frame
composites executed script-side:

- `state.snapshot` → `{ppq, tempo*, playing, song_pos, current_pattern, pattern_count,
  channels: [{idx, name, muted, vol, pan, target_mixer_track}], mixer: [{idx, name,
  vol, pan, muted, solo}] }` (*tempo read via `mixer.getCurrentTempo()`
  [ASSUMED name - Spike 3]).
- `ctl.set_mutes([[channel, 0|1], ...])` → `[[channel, muted], ...]`: sets many channel
  mutes explicitly in one round trip and reads each back; a solo or mute render uses it
  once each way (issue #309).
- `state.peaks([track, ...])` → `[[track, peak], ...]`: every requested mixer track's
  current meter peak (`mixer.getTrackPeaks`, louder of left and right, 1.0 = 0 dBFS) in
  one round trip; each `fl.meters` sweep is one call (issue #317).
- `state.plugin_params(chan_or_track)` → `[{idx, name, value}]` - built from
  `plugins.getParamCount/getParamName/getParamValue`. Potentially thousands of params
  (VSTs) - paginated by the op's args and chunked in the response.

## Control-plane capability inventory (v1)

What the MCP server may call through this script, grouped by FL module - the concrete
subset we commit to wiring and testing, chosen against the API Stubs docs:

| Module | v1 functions |
|---|---|
| `transport` | `start`, `stop`, `record`, `isPlaying`, `getSongPos`, `setSongPos`, `setLoopMode`, `globalTransport` |
| `patterns` | `patternNumber`, `patternCount`, `jumpToPattern`, `getPatternName`, `setPatternName`, `getPatternLength` |
| `channels` | `channelCount`, `selectOneChannel`, `getChannelName`, `setChannelName`, `getChannelVolume/Pan` + setters, `muteChannel`, `getGridBit`, `setGridBit`, `getTargetFxTrack` |
| `mixer` | `trackCount`, `getTrackName/setTrackName`, `getTrackVolume/setTrackVolume`, `getTrackPan/setTrackPan`, `muteTrack`, `soloTrack`, `getRouteSendActive/setRouteTo`, `getCurrentTempo` |
| `plugins` | `isValid`, `getPluginName`, `getParamCount`, `getParamName`, `getParamValue`, `setParamValue`, `getPresetCount`, `nextPreset`, `prevPreset` |
| `general` | `getRecPPQ`, `undo`, `undoUp`, `saveUndo`, `getVersion`, `processRECEvent` (tempo set - see below) |
| `ui` | `setHintMsg`, `crDisplayRect`*, window show/hide for piano roll & mixer, `showWindow`, keyboard emulation for Ctrl+S (used only by `sync_from_project`) |
| `arrangement` | `jumpToMarker`, `getMarkerName`, `currentTime`, `selectionStart/End` |

(*Exact function names are normalized against the API Stubs during Spike 3; this table
fixes the *capability* commitment, not the final spelling.*)

### Known tricky operations

- **Set tempo**: no direct setter exists. The known technique is
  `general.processRECEvent(midi.REC_Tempo, bpm*1000, midi.REC_Control | midi.REC_UpdateControl)`
  [REPORTED - community scripts]. Validated in Spike 3; if unstable we fall back to
  `transport.globalTransport(FPT_TempoJog, …)` stepping.
- **Save project** (`sync_from_project` support): no `general.save()`. Route:
  `ui` keyboard emulation of Ctrl+S [ASSUMED - Spike 3; fallback: instruct user +
  detect mtime change on the project file].
- **Selecting mixer track for `plugins.*`**: plugin addressing is `(index, slotIndex)`
  where index is channel # or mixer track # - the server tracks which addressing form
  each target needs (channel rack instrument vs mixer effect).

## Registration & handshake UX

FL requires the user to (once): enable the script under MIDI settings - set `FLMCP Req`
input to controller type "FLMCP Bridge Script" with a port number, and add `FLMCP Res`
as output with the matching port. The installer documents this with screenshots; the
`hello` event doubles as the "it worked" signal, which the MCP server surfaces as a
green check in its startup diagnostics (`doctor` tool, 06).
