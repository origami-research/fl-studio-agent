"""The checked-in contract is exactly what the engine publishes."""

from __future__ import annotations

import json
from pathlib import Path

import anyio

from fl_mcp.contract import BRIDGE_PORT_FILE, ENV_AGENT_PROJECT, ENV_SPOOL_ROOT, build_contract, contract_json
from fl_mcp.server import create_server

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
