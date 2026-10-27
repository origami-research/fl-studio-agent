"""Start the MCP server over stdio and check its sandbox as a client sees it (docs/38).

Usage: python macos_smoke.py SERVER_COMMAND [ARGS...] --workspace-only

The server must say confined=true, write inside its workspace, and refuse
writes outside it through both write_file and shell. CI runs it against the
checkout (ci.yml macos-sandbox) and the release runs it against the package
uvx installs from PyPI (release.yml macos-smoke).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OUTSIDE = Path.home() / "flmcp-smoke-outside.txt"


async def main(command: list[str]) -> None:
    server = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        info = (await session.initialize()).server_info
        print("serverInfo:", info.description)
        assert info.description.endswith("; confined=true"), info.description
        inside = await session.call_tool("write_file", {"path": "inside.txt", "content": "ok"})
        assert not inside.is_error, inside.content
        refused = await session.call_tool("write_file", {"path": str(OUTSIDE), "content": "escape"})
        print("write_file outside:", refused.content[0].text[:300])
        assert refused.is_error, refused.content
        shell = await session.call_tool("shell", {"command": f"echo escape > {OUTSIDE}"})
        result = json.loads(shell.content[0].text)
        print("shell outside:", result["exit_code"], result["stderr"][:300])
        assert result["exit_code"] != 0 and "Operation not permitted" in result["stderr"], result
    assert not OUTSIDE.exists(), f"{OUTSIDE} was written"
    print("confined: writes outside the workspace are refused")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
