"""FLSession — ties the v1 planes and records together for one server run.

The control plane is whatever the caller passes as ``control``: production
wires ``control_midi.get_control()`` (the live, server-hosted MIDI transport —
docs/14); tests inject a ``fake_fl.FakeControl``. Either way this module only
needs the shared surface — ``send_request``/``add_event_listener``/
``await_hello``/``ping`` — not a concrete transport type, so it never
constructs one itself. ``control=None`` (the default) means "no control plane
configured for this session" and every control-dependent call fails with a
structured ``control_plane_unavailable`` error.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from . import actions, errors, records, schemas, spool


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ControlPlane(Protocol):
    """What FLSession needs from a control transport — see module docstring."""

    def send_request(
        self, op: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None, timeout: float = 2.0
    ) -> schemas.ControlResponse: ...

    def add_event_listener(self, fn: Any) -> None: ...

    def await_hello(self, timeout: float = 5.0) -> schemas.HelloEvent: ...

    def ping(self, timeout: float = 2.0) -> bool: ...


class FLSession:
    def __init__(
        self,
        project_key: str = "default",
        spool_root: Path | None = None,
        project_root: Path | None = None,
        control: ControlPlane | None = None,
    ) -> None:
        self.project_key = project_key
        self.spool = spool.SpoolSession(root=spool_root)
        self.shadow = records.ShadowStore(project_key, root=project_root)
        self.actions = actions.ActionLog(project_key, root=project_root)
        self.control = control

    def write_notes(
        self,
        target: schemas.Target,
        notes: list[schemas.Note],
        mode: str = "merge",
        timeout: float = 2.0,
    ) -> tuple[schemas.SpoolResponse, schemas.SpoolCommand]:
        """Send add_notes, wait for the Bridge plugin's confirmation, write through on success."""
        cmd = self.spool.send_command(
            "add_notes",
            target=target.model_dump(mode="json"),
            mode=mode,
            notes=[json.loads(n.model_dump_json()) for n in notes],
        )
        resp = self.spool.wait_for_response(cmd.id, timeout=timeout)
        if resp.ok:
            ref = self.shadow.write(target, notes, mode=mode, provenance="shadow")
            self.actions.append(
                schemas.ActionEntry(
                    ts=_now(),
                    id=cmd.id,
                    gen=cmd.gen,
                    op="add_notes",
                    target=target,
                    mode=mode,
                    after_hash=ref.id,
                    undo_step=f"mcp: write notes ({target.pattern}:{target.channel})",
                )
            )
        return resp, cmd

    def clear_notes(
        self, target: schemas.Target, timeout: float = 2.0
    ) -> tuple[schemas.SpoolResponse, schemas.SpoolCommand]:
        cmd = self.spool.send_command("clear_notes", target=target.model_dump(mode="json"))
        resp = self.spool.wait_for_response(cmd.id, timeout=timeout)
        if resp.ok:
            before = self.shadow.ref_at(target)
            ref = self.shadow.clear(target, provenance="shadow")
            self.actions.append(
                schemas.ActionEntry(
                    ts=_now(),
                    id=cmd.id,
                    gen=cmd.gen,
                    op="clear_notes",
                    target=target,
                    before_hash=before.id if before else None,
                    after_hash=ref.id,
                    undo_step=f"mcp: clear notes ({target.pattern}:{target.channel})",
                )
            )
        return resp, cmd

    def doctor(self) -> dict:
        """Status of every plane, per docs/06's fl_doctor contract.

        - control_plane: the live MIDI transport (docs/14) — connected iff the
          controller script has said hello. If it hasn't and FL Studio's
          process is running, that's the docs/15 fix-item-5 port-ordering
          trap (ports must exist before FL launches) — named explicitly so
          the fix is "restart FL Studio once", not a bug report.
        - note_plane: the v0 interim path (docs/15 fix item 4 keeps notes on
          this path) — connected iff the piano-roll script is installed.
        - template_project: deferred pending a licensed FL Studio install
          (product decision, this merge) — always "pending", never a
          PASS/FAIL/blocking WARN.
        """
        from .install import script_installed_path  # local import: optional dependency on FL paths

        report: dict[str, object] = {"status": "ok"}

        if self.control is None:
            report["control_plane"] = {
                "connected": False,
                "detail": "no control-plane transport configured for this session",
            }
        else:
            try:
                hello = self.control.await_hello(timeout=0.2)
                report["control_plane"] = {
                    "connected": True,
                    "fl_ver": hello.fl_ver,
                    "script_ver": hello.script_ver,
                    "ppq": hello.ppq,
                }
            except errors.ToolError as exc:
                detail = exc.to_dict()
                if _fl_process_running():
                    detail["fix"] = (
                        "FL Studio is running but hasn't connected yet. The virtual MIDI ports "
                        "must exist before FL Studio launches (docs/14) — restart FL Studio once."
                    )
                report["control_plane"] = {"connected": False, **detail}

        installed = script_installed_path() is not None
        report["note_plane"] = {
            "connected": installed,
            "mode": "v0 (interim piano-roll script, manual trigger — docs/15 fix item 4)",
            "detail": (
                "piano-roll script installed"
                if installed
                else "piano-roll script not installed - run: python -m fl_mcp.install"
            ),
        }

        report["template_project"] = {
            "status": "pending",
            "detail": "Deferred until a licensed FL Studio install is available. Not a failure.",
        }

        if not report["control_plane"].get("connected") or not report["note_plane"].get("connected"):  # type: ignore[union-attr]
            report["status"] = "degraded"
        return report


def _fl_process_running() -> bool:
    """Best-effort check for a running FL Studio process (docs/15 fix item 5).

    Used only to sharpen fl_doctor's message; a False here just means "can't
    tell" (unsupported platform, or no process-listing tool), never a claim
    that FL definitely isn't running.
    """
    import subprocess
    import sys

    if sys.platform != "darwin":
        return False  # Windows/other: process check not implemented yet (docs/08 roadmap)
    try:
        result = subprocess.run(["pgrep", "-fi", "FL Studio"], capture_output=True, timeout=2)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
