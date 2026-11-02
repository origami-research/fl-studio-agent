"""The run kernel: model code runs in its own long-lived Python process (docs/38).

``host`` is the MCP-server side, ``child`` the kernel process, ``wire`` the JSON
protocol between them, ``cell`` the shared cell executor, ``display`` the images
a cell shows (``mpl_inline`` routes matplotlib's into it), ``launch``,
``windows`` and ``seatbelt`` the per-OS process launch that is the security
boundary, and ``probe`` the check that the boundary holds.
"""
