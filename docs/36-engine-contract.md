# Engine contract

`contracts/fl-mcp.json` is the machine-readable MCP contract published by this
engine. Generate it with:

```bash
cd mcp-server
uv run fl-mcp-contract > ../contracts/fl-mcp.json
```

The contract is built from the registered MCP tools and their Pydantic input
and output models. It also publishes the supported session environment names
and the Bridge port-file schema. The test suite fails when the checked-in file
does not match the generated contract.

Tools publish only their success model as `outputSchema`. Anticipated engine
failures are `ToolErrorResult` JSON in the text content of MCP `isError` tool
results, not an alternate successful output shape. The server returns an
explicit MCP `CallToolResult` for these failures because the installed SDK
otherwise prefixes raised exceptions. Consumers must check `isError` before
decoding a tool result against its output schema.

## Session audio relay

The machine-readable contract is unchanged by the optional live audio relay.
Arena sessions set `FLMCP_AUDIO_STREAM=1`; after the Bridge heartbeat is live,
the session publishes `<FLMCP_SPOOL_ROOT>/bridge_audio.json` with loopback
`host`, `port`, `proto`, and `sample_rate`. Consumers use that file with
`fl-mcp-listen` and must tolerate replacement when the Bridge restarts. The
file disappears when the session closes.
