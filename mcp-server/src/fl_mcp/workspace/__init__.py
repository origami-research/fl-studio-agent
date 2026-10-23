"""The workspace tools: shell and file tools confined to the session's workspace (docs/39).

``ops`` is what runs inside the confined process (stdlib only), ``op`` its
stdin/stdout entry point, ``runner`` the server side that launches one
confined process per call through the run kernel's launcher.
"""
