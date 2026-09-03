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
