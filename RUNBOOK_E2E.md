# RUNBOOK: the live E2E matrix window

This is the orchestrator's script for running the FLP project matrix against real FL Studio in a supervised machine window.
Tier 1 (headless) needs no window and no FL process - it is part of the default fast suite.
Tier 2 (live) boots FL, loads every matrix project, reads state back through FL's own control plane, and finishes with a headless render.

The matrix and the shared cycle live in `mcp-server/tests/e2e/matrix.py`.
The six project shapes: minimal template with event 172, the 4-pattern chord template, an 80-byte-stride demo with automation clips, a legacy 32-byte-stride demo, the 308-channel Astes demo, and a synthetic two-arrangement project.
The demo picks are programmatic (by decoded playlist stride), so the matrix follows the installed FL version.

## Preconditions

- FL Studio 2026 installed and licensed, welcome screen disabled (it blocks controller init - docs/21 addendum 10).
- macOS Accessibility/Automation consent granted to the terminal (`harness/enable_autonomy.py`), so modal dismissal works.
- FL Studio CLOSED before starting (the fixtures stop a harness-started FL themselves, but refuse to kill a session they did not start).
- The venv exists: `cd mcp-server && uv sync && uv pip install --python .venv/bin/python -e ..`.
- Nothing else is using the FLMCP virtual MIDI ports.

## Commands

All commands run from `mcp-server/`.

Tier 1 - headless matrix (no FL process; also runs inside the default `pytest`):

```sh
.venv/bin/python -m pytest tests/e2e/test_matrix.py -q
```

Expected: `6 passed` in well under 60s.

Tier 2 - the full live window (~20 minutes budgeted):

```sh
FL_E2E=1 .venv/bin/python -m pytest tests/e2e -m e2e -q
```

Expected: the control-plane e2e (6 tests), the live matrix (6 tests, one FL boot, projects opened sequentially), then the render gate (1 test, FL closed, a WAV appears next to the tmp project).
Every live matrix test re-runs the tier-1 cycle first, so a file-lane defect fails before FL is asked to testify.

## Trimming a short window

| Variable | Effect | Example |
|---|---|---|
| `FL_E2E` | gates every live test; unset = all live tests skip | `FL_E2E=1` |
| `FL_E2E_MATRIX` | comma-separated matrix ids for the live cycle | `FL_E2E_MATRIX=minimal-basic-with-limiter,chord-progression` |
| `FL_E2E_RENDER` | matrix ids to render headlessly (default `chord-progression`) | `FL_E2E_RENDER=chord-progression,synthetic-multi-arrangement` |

Matrix ids: `minimal-basic-with-limiter`, `chord-progression`, `demo-80-stride-automation`, `demo-32-stride-legacy`, `monster-astes-308ch`, `synthetic-multi-arrangement`.

Trim order when the window is short: drop `monster-astes-308ch` first (longest load), then the two demo picks; the two templates plus the synthetic are the minimum that still exercises clip writes, arrangement targeting, and event 172.
Rendering a full demo song can take minutes; keep `FL_E2E_RENDER` at its default unless the window has slack.

## Reading a failure

Every failure names the matrix project and the cycle step, e.g. `[demo-32-stride-legacy / step:surgical-invariant] ...`.

- `step:copy-to-tmp` .. `step:surgical-invariant` - a FILE-LANE defect; reproduce headless with tier 1, no FL window needed.
- `controller never said hello` - FL booted without binding the ports or a modal is up; check the welcome screen, then relaunch the window.
- `FL never showed <name>.flp in any window title` - FL likely REJECTED the written file (fact 5's trap: readbacks from the default project look plausible, which is why the title check exists). Keep the tmp `.flp` for byte-level dissection; this is the highest-value failure the matrix can produce.
- `step:live-tempo-readback` mismatch - FL is not looking at our file (a silently-failed project switch shows the PREVIOUS project's marker), or tempo write is broken; the per-project unique marker distinguishes the two.
- `step:live-levels-readback` mismatch - the levels patch did not land or the byte map drifted; compare with docs/21 addendum A8.
- `step:headless-render` - `OsxFL -R` produced nothing; confirm FL was fully closed (the E4 finding requires no GUI session) and re-run just the render module.
- `no bundled project matches this shape` - the installed FL's corpus changed (update or reinstall); the matrix picks found no file of that shape. Record it as a finding and re-pin the matrix.

## After the window

- The fixtures SIGKILL the FL they started and close the ports on teardown; nothing else to clean.
- All written files live under pytest tmp dirs; FL's bundled Data files are never touched.
- Paste the pytest tail (pass/fail counts plus any `[project / step:...]` lines) into the PR or issue that requested the window.
