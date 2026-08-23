# Setup: from zero to "ask for notes, they appear in FL Studio"

Run this any time to see exactly where you are:

```bash
mcp-server/.venv/bin/python -m harness.doctor
```

It checks every link and names the fix for the first broken one.

---

## The honest summary

Writing notes into FL works today and is fully tested. Making it apply **without
you clicking** needs two one-time steps that macOS and FL Studio each require a
human for. Neither can be scripted, and it is worth knowing why before you spend
time on them.

| Step | Automatable? | Why not |
|---|---|---|
| Install the piano roll script | yes, done | — |
| Queue notes from an agent | yes, done | — |
| **Grant macOS automation consent** | **no** | The TCC permission database is SIP-protected. `tccutil` can reset entries but never grant them. Consent is deliberately gated behind a human click. |
| **Bind a hotkey to the script in FL** | **no** | FL assigns no shortcut to piano roll scripts automatically, and stores the binding somewhere with no writable file to pre-populate. |

The design that would have avoided both — a native plugin polling FL's Idle
callback, needing no trigger at all — is blocked: FL Studio does not load
third-party plugins from `Plugins/Fruity/Effects`. That is measured, not
assumed; see [docs/11](docs/11-native-plugin-blocked.md).

---

## Step 1 — install (already done, repeat after a code change)

```bash
cd mcp-server && python -m fl_mcp.install    # then restart FL Studio
```

## Step 2 — grant macOS automation

```bash
mcp-server/.venv/bin/python -m harness.enable_autonomy
```

This provokes the consent prompt (until an app *attempts* automation it does not
appear in the list at all), opens the right System Settings panes, waits for the
grant, then verifies.

Enable, under the app running this (Terminal / iTerm / Claude):

- **Privacy & Security ▸ Automation** → `System Events` and `FL Studio`
- **Privacy & Security ▸ Accessibility** → the same app

## Step 3 — bind the hotkey inside FL

In FL Studio's piano roll: **menu ▸ Tools ▸ Scripting**, right-click
**MCP Write Notes**, assign **Cmd+Opt+Y**.

Confirm by pressing Cmd+Opt+Y yourself — the script should run. Then record it
so `doctor` stops asking:

```bash
touch harness/.hotkey_bound
```

## Step 4 — restart the MCP server

Auto-apply was added after your current server process started, so it is running
older code:

```bash
claude mcp restart fl-studio     # or restart the session
```

## Step 5 — confirm

```bash
mcp-server/.venv/bin/python -m harness.doctor
```

All green means asking for notes writes them straight into the open piano roll.
`write_notes` will return `"status": "applied"` instead of `"queued"`.

---

## Using it before finishing setup

Everything works now with one click per request:

1. Ask for notes.
2. In the piano roll: **menu ▸ Tools ▸ Scripting ▸ MCP Write Notes**.

That path is fully tested — 46 automated tests, including the real `.pyscript`
run against an emulation of FL's sandbox
([docs/10](docs/10-v0-vertical-slice.md)).

## For development

```bash
cd mcp-server && .venv/bin/python -m pytest -q      # 46 tests
.venv/bin/python tests/mutation_check.py            # verifies the FL sandbox emulator
python -m harness.flctl                             # FL lifecycle control
python -m harness.verify <project.flp>              # ground-truth notes from a saved project
```
