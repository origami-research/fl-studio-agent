"""Live-FL end-to-end fixtures, built on the one FLHost harness (fl_host.py).

These run the ACTUAL server code against a real FL Studio session and assert
closed-loop using FL's own read-back - no fake, no human eyeballing.

They never run in normal CI: `fl_host` skips unless FL_E2E=1 is set AND FL
Studio is installed, so `pytest tests/` on a CI box (or a dev machine you
don't want hijacked) collects them as skips. Run them deliberately with:

    FL_E2E=1 .venv/bin/python -m pytest tests/e2e -m e2e

(See RUNBOOK_E2E.md at the repo root for the orchestrator's full live window.)

Lifecycle (docs/14 ordering): FLHost opens the control-plane ports FIRST,
then launches FL so it binds them at startup, waits for the controller's
hello (dismissing any trial/welcome modal), and stops FL + closes ports on
teardown. Module scope so a module that must run with FL closed (the render
gate) is not held hostage by a session-long FL.
"""

from __future__ import annotations

import os
from typing import NamedTuple

import pytest

from e2e.fl_host import FLHost
from fl_mcp.flpaths import fl_app
from fl_mcp.session import FLSession, create_session


class LiveFL(NamedTuple):
    """What the live fixture provides: the wired session plus FL's hello event.

    A named tuple instead of stapling the hello onto the session as an
    undeclared attribute - fixtures return values, they don't smuggle state.
    """

    session: FLSession
    hello: dict


@pytest.fixture(scope="module")
def fl_host():
    if not os.environ.get("FL_E2E"):
        pytest.skip("live-FL e2e disabled; set FL_E2E=1 to run")
    if not fl_app().exists():
        pytest.skip("FL Studio not installed")

    host = FLHost()
    host.launch()
    yield host
    host.close()


@pytest.fixture(scope="module")
def live_fl(fl_host):
    # v2 FLSession takes its collaborators explicitly - no hidden construction.
    session = create_session()
    if session.control is not None:
        session.control.close()  # the host owns the transport, not the factory
    session.control = fl_host.ctl
    return LiveFL(session=session, hello=fl_host.hello)
