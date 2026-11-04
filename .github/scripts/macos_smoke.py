"""Start the MCP server over stdio and check its sandbox as a client sees it (docs/38).

Usage: python macos_smoke.py SERVER_COMMAND [ARGS...] --workspace-only

The server must say confined=true, serve one tool (run), write inside its
workspace, and refuse writes outside it from Python and from a shell command.
CI runs it against the checkout (ci.yml macos-sandbox) and the release runs it
against the package uvx installs from PyPI (release.yml macos-smoke).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OUTSIDE = Path.home() / "flmcp-smoke-outside.txt"


async def run(session: ClientSession, code: str) -> dict:
    result = await session.call_tool("run", {"code": code})
    return json.loads(result.content[0].text)


async def main(command: list[str]) -> None:
    server = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        info = (await session.initialize()).server_info
        print("serverInfo:", info.description)
        assert info.description.endswith("; confined=true"), info.description
        tools = [tool.name for tool in (await session.list_tools()).tools]
        assert tools == ["run"], tools
        inside = await run(session, "open('inside.txt', 'w').write('ok')")
        assert inside["outcome"] == "ok", inside
        refused = await run(session, f"open({str(OUTSIDE)!r}, 'w').write('escape')")
        print("open outside:", (refused.get("traceback") or "")[-300:])
        assert refused["outcome"] == "error" and "PermissionError" in refused["traceback"], refused
        shell = await run(
            session,
            "import subprocess\n"
            f"r = subprocess.run('echo escape > {OUTSIDE}', shell=True, capture_output=True, text=True)\n"
            "(r.returncode, r.stderr)",
        )
        print("shell outside:", shell["result"][:300])
        assert "Operation not permitted" in shell["result"] and not shell["result"].startswith("(0,"), shell
    assert not OUTSIDE.exists(), f"{OUTSIDE} was written"
    print("confined: writes outside the workspace are refused")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
