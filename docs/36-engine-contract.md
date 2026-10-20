# Engine contract

`contracts/fl-mcp.json` is the machine-readable MCP contract published by this
engine. Generate it with:

```bash
cd mcp-server
uv run fl-mcp-contract > ../contracts/fl-mcp.json
```

The contract is built from the registered MCP tools and their Pydantic input
and output models. It also publishes the supported session environment names
and the Bridge port-file schema. `FLMCP_KERNEL_ROOT` is the `run` kernel's
workspace root; [38](38-python-kernel.md) has the box contract around it. The test suite fails when the checked-in file
does not match the generated contract.

Tools publish only their success model as `outputSchema`. Anticipated engine
failures are `ToolErrorResult` JSON in the text content of MCP `isError` tool
results, not an alternate successful output shape. The server returns an
explicit MCP `CallToolResult` for these failures because the installed SDK
otherwise prefixes raised exceptions. Consumers must check `isError` before
decoding a tool result against its output schema.

## Actions

Every tool result (`run`, `get_scene`, `check`, `render`, `analyze`) carries `actions`: the plain sentences a UI can show as "what the assistant did".
There is one past-tense sentence per change FL confirmed or measurement the engine took during the call, in order.
Sentences are complete, listing every item; a UI shows one line and expands it, and only a pathological sentence past 2000 characters is cut with "...".
Examples: "Set tempo to 122 BPM", "Replaced the notes on FLEX Bass (212 notes)", "Checked bars 9-10: 5 of 5 tracks sound, master -1.8 dBFS", "Rendered 0:34".
Sentences are built from what each call returned (FL's values), never from what was asked for.
Reads add none, so `get_scene` always returns an empty list, and a failed call adds none: it stays an error.
Only changes in a row merge, so the list keeps FL's order and each sentence is true of the state it describes.
Three or more changes of one kind in a row become one sentence, such as "Set volume on 3 tracks: kick 0.60, clap 0.42, hat 0.30"; a later value for the same track replaces the earlier one, and merged notes add up.
Repeated undos read "Undid 4 actions in FL", and an identical sentence repeated in a row is counted ("Saved the song (3 times)").
Changes are told apart by channel and pattern, not by name: two channels with one name are listed as "Sampler (channel 0)" and "Sampler (channel 2)".
A call lists at most 200 actions, then "and N more actions", because a cell can loop on `fl` calls without using its time budget.
For `run`, the server records each `fl` call it serves while the cell runs, so a cell that raises partway, is interrupted, or loses its kernel still lists the changes FL already has.
Inside `fl.file_edit()`, changes reach FL only when the block closes and FL reopens the changed song, so they are listed then, followed by "Reopened FL with the changed song"; if closing fails, none of them are listed.
`fl.analyze` and `fl.read_wav` run inside the kernel without reaching the server, so a cell's measurements are not listed; the `analyze` tool's are.
`fl_mcp/actions.py` holds the wording.

## Session audio relay

The machine-readable contract is unchanged by the optional live audio relay.
Arena sessions set `FLMCP_AUDIO_STREAM=1`; after the Bridge heartbeat is live,
the session publishes `<FLMCP_SPOOL_ROOT>/bridge_audio.json` with loopback
`host`, `port`, `proto`, and `sample_rate`. Consumers use that file with
`fl-mcp-listen` and must tolerate replacement when the Bridge restarts. The
file disappears when the session closes. The relay shares the session command
transport, but its send-and-await exchange is serialized with prewarm and
tool exchanges, so enabling it never races MCP initialization.
