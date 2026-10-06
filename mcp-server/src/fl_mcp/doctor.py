"""``fl-mcp-doctor``: the fl_doctor report for this user's running FL, as JSON.

It builds the same session the MCP server builds (same environment, same
defaults), so a box install can prove what a later ``fl-mcp`` will see without
an MCP client. Exit 0 only when every plane is connected.
"""

from __future__ import annotations

from fl_mcp.session import create_session


def main() -> int:
    session = create_session()
    try:
        report = session.doctor()
    finally:
        session.close()
    print(report.model_dump_json())
    return 0 if report.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
