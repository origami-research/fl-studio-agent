"""The files FL Studio loads, shipped inside the server wheel.

``device_FLMCP.py`` is the MIDI controller script (it embeds ``flmcp_codec``
verbatim so FL can load it as one standalone file) and ``MCP Write Notes.pyscript``
is the piano-roll script. ``fl-mcp-install`` copies both out of the installed
package into FL's user folders; the server imports ``flmcp_codec`` directly.
"""
