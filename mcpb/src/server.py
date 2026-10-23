"""Claude Desktop bundle entry point: run the fl-studio-agent server that the
bundle's pyproject.toml pins (installed from PyPI by the host's uv)."""

from fl_mcp.server import main

if __name__ == "__main__":
    main()
