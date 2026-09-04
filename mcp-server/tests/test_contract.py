"""The checked-in contract is exactly what the engine publishes."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import get_type_hints

import anyio
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from fl_mcp import server
from fl_mcp.contract import BRIDGE_PORT_FILE, ENV_AGENT_PROJECT, ENV_SPOOL_ROOT, build_contract, contract_json
from fl_mcp.errors import ToolError
from fl_mcp.server import create_server, tool_definitions

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "fl-mcp.json"


def test_checked_in_contract_is_current():
    checked_in = json.loads(CONTRACT_PATH.read_text())
    generated = build_contract()
    assert checked_in == generated, (
        "contract changed, regenerate and bump: uv run fl-mcp-contract > ../contracts/fl-mcp.json"
    )


def test_contract_configuration_vocabulary_is_centralized():
    contract = contract_json()
    assert ENV_SPOOL_ROOT in contract
    assert ENV_AGENT_PROJECT in contract
    assert BRIDGE_PORT_FILE in contract
    assert "FLMCP_UDP_PORT" not in contract


def test_live_tool_schemas_match_the_published_contract():
    published = {tool["name"]: tool["outputSchema"] for tool in build_contract()["tools"]}
    live = anyio.run(create_server().list_tools)

    assert {tool.name: tool.model_dump(by_alias=True)["outputSchema"] for tool in live} == published


def test_tool_output_schemas_are_their_flat_success_models():
    """Errors use MCP isError results, so successful tool schemas never wrap a union."""
    published = {tool["name"]: tool["outputSchema"] for tool in build_contract()["tools"]}

    for fn in tool_definitions():
        success_model = get_type_hints(fn)["return"]
        expected = success_model.model_json_schema()
        actual = published[fn.__name__]
        assert actual.get("properties", {}) == expected.get("properties", {}), fn.__name__
        assert "result" not in actual.get("properties", {}) or "result" in success_model.model_fields


def test_tool_error_reaches_an_mcp_client_as_parseable_is_error(monkeypatch):
    """An anticipated engine failure remains structured after the MCP boundary."""

    class FailingSession:
        def read_notes(self, _target):
            raise ToolError("note_plane_unavailable", "bridge offline", "start FL", retryable=True)

    @contextlib.asynccontextmanager
    async def fake_lifespan(_mcp_server):
        yield {"session": FailingSession()}

    monkeypatch.setattr(server, "lifespan", fake_lifespan)

    async def call() -> None:
        async with InMemoryTransport(create_server()) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("get_notes", {"pattern": 1, "channel": 0})

        assert result.is_error
        assert json.loads(result.content[0].text) == {
            "status": "error",
            "error": "note_plane_unavailable",
            "message": "bridge offline",
            "fix": "start FL",
            "retryable": True,
            "candidates": None,
        }

    anyio.run(call)


def test_tools_list_validates_through_the_mcp_layer_and_every_output_schema_is_an_object(monkeypatch):
    """The SDK validates its own tools/list result; a root-level union schema made
    that fail (INTERNAL_ERROR "Handler returned an invalid result"), so no client
    could list tools. Drive tools/list through a real client to guard it."""

    @contextlib.asynccontextmanager
    async def fake_lifespan(_mcp_server):
        yield {}

    monkeypatch.setattr(server, "lifespan", fake_lifespan)

    async def call():
        async with InMemoryTransport(create_server()) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            return await client.list_tools()

    listed = anyio.run(call)
    tools = {tool.name: tool.model_dump(by_alias=True) for tool in listed.tools}
    assert set(tools) == {"run", "get_notes", "get_scene", "render", "analyze", "fl_doctor"}
    for name, tool in tools.items():
        schema = tool["outputSchema"]
        assert schema.get("type") == "object", f"{name}: output schema must be an object, got {schema.get('type')!r}"
        assert "properties" in schema, f"{name}: output schema has no properties"
