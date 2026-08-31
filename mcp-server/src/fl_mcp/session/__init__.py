"""One object owning the connection to FL: control wire + note spool + project.

The public seam (docs/27 §7) is exactly these four names, byte-identical for
sdk.py, server.py, and every caller. The division of labor is by CAPABILITY,
chosen per call from evidence (docs/27 §2; there is no flag):

- facade.py    - FLSession (thin delegation) + create_session()
- link.py      - ControlLink: wire liveness, hot-plug self-heal, settle
- lifecycle.py - reload_fl (the quarantined SIGKILL + reopen)
- router.py    - Capabilities + detect() + BridgeBreaker (the evidence; the
                 routing rule is fl_mcp.core.router.route via fl_mcp.ops)
- notes.py     - FileLane + BridgeLane (docs/30 Lanes) + read_notes
- controls.py  - tempo, levels, plugin params
- state.py     - snapshot, channel_names, doctor
"""

from fl_mcp.errors import NotePlaneUnavailable, WriteRejected

from .facade import FLSession, create_session

__all__ = ["FLSession", "NotePlaneUnavailable", "WriteRejected", "create_session"]
