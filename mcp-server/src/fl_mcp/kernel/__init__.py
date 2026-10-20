"""The run kernel: model code runs in its own long-lived Python process (docs/38).

``host`` is the MCP-server side, ``child`` the kernel process, ``wire`` the JSON
protocol between them, ``cell`` the shared cell executor, ``launch`` and
``windows`` the per-OS process launch that is the security boundary.
"""
