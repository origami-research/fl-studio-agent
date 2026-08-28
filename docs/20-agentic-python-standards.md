# Agentic Python standards: the bad code AI writes, the proper code, and how we stop it

This is the durable standard behind `AGENTS.md` and the plan in `docs/19`.
It catalogs the anti-patterns that LLM coding agents reliably produce in Python -
researched from the field literature, not invented here - pairs each with the
proper idiom, points at where *our own* code has it, and names the guard that
keeps it from coming back.

The point: our audit findings aren't a local mess, they're the textbook agentic
failure modes.
Fixing them once is worth little; encoding the standard + the gate is the work.

## Why agents produce this (the mechanism, so the rules make sense)

An LLM generates what *looks* correct from the prompt and whatever fits the
context window.
Four consequences follow, and every pattern below is downstream of one of them:

- **It doesn't search the codebase.** Shared utilities that live elsewhere
  effectively don't exist, so it re-writes them. Across 211M analyzed lines,
  copy/paste rose 8.3% → 12.3% while refactoring fell 25% → under 10%.
- **It pattern-matches training data.** If the common "handle input" example
  lacks validation, the output lacks validation (Snyk: 36-40% of AI code carries
  a vulnerability). If the common "handle error" example is `except: pass`, so is
  the output.
- **It optimizes for "looks right / passes the basic test."** ~60% of AI code
  faults are *silent*: they compile, pass tests, and return the wrong result.
- **It claims done without verifying.** "This should pass" is emitted before the
  test is run - the exact failure mode that let `set_tempo` merge green against a
  fake while broken against real FL.

## The catalog

Each entry: the anti-pattern, the proper Python, where we have it, the guard.

### 1. Silent failure / plausible-but-wrong (the #1 agentic bug)

Code that looks right, passes the fake, returns a wrong result.
The boundary reports what it *hoped for* instead of what it *confirmed*.

```python
# BAD - reports the intended count, never checks what persisted
pyflp.save(project, path)
return {"ok": True, "notes": len(blob) // NOTE_SIZE}

# GOOD - read back and report the confirmed state
pyflp.save(project, path)
written = count_notes(parse(path), pattern, channel)
return Applied(notes=written)
```

- **Ours:** `flp_writer.write_notes` (claims a re-read it never does),
  `set_tempo` live tier (reports requested bpm, not `getCurrentTempo()`),
  `set_channel_mix` (partial live edit discarded by a reload).
- **Guard:** boundaries read back; the real-FL e2e asserts the *confirmed* value,
  never the requested one. The fake cannot catch this by construction.

### 2. Hiding exceptions (`except: pass` / `except Exception: return None`)

Silent swallow - the error never reaches logs, Sentry, or the caller.

```python
# BAD
try:
    ids = json.loads(text)["ids"]
except Exception:
    pass  # -> treated as empty -> replays an applied command

# GOOD - log at a real level, or convert to a typed result; fail closed on a
# safety-critical read
try:
    ids = json.loads(text)["ids"]
except (OSError, json.JSONDecodeError):
    logger.exception("consumed-ledger unreadable; refusing to consume")
    return LedgerUnreadable()   # fail closed, don't assume "nothing applied"
```

- **Ours:** `MCP Write Notes.pyscript:_consumed_ids` (corrupt ledger → replay),
  `spool.next_command_id` persist failure, `spool.read_json`,
  `autonomous.list_channels` (`except Exception` masks PyFLP bugs as "unnamed").
- **Guard:** ruff `BLE`; a genuine ignore is `contextlib.suppress(Specific)` with
  a reason; a safety read fails closed, not open.

### 3. Unconstrained defensive programming (`getattr` default, `x or default`)

Handling an edge case as a generic case at a low level, hiding it from the caller.

```python
# BAD - a missing tempo becomes a plausible default; the failure vanishes
tempo = getattr(project, "tempo", None) or 120.0

# GOOD - trust the type; surface the real gap as data, don't paper over it
if project.tempo is None:
    program.warnings.append("tempo unreadable from project")
else:
    program.steps.append(SetTempo(bpm=project.tempo))
```

- **Ours:** `getattr` is already at 0 (prior pass). Residual: `channel.name or ""`
  under a broad suppress, and any `.get(k, fallback)` that silently changes behavior.
- **Guard:** ruff bans `getattr`-default patterns in review; raise/surface at the
  low level, decide recovery only where context exists.

### 4. Stringly-typed data (`dict[str, Any]`, `str | int` unions, magic sentinels)

No shape a caller can trust, so every consumer re-parses or reaches for `.get`.

```python
# BAD - the union is re-resolved in every consumer
key: str | int
notes = [n for n in notes if n.key >= key_min]  # "C4" >= 48 raises

# GOOD - resolve once at the boundary; downstream gets one firm type
key: int = Field(ge=0, le=127)
@field_validator("key", mode="before")
def _coerce(cls, v: str | int) -> int: return parse_key(v)
```

- **Ours:** the 11-item type inventory in `docs/19` - `Note.key`, `Target`'s
  `-1` sentinel, `mode: str`, `dict|None` backend returns, untyped control-plane
  RPC, `SpoolCommand.args`, `ActionEntry.before/after`, `decompile.Step.args`,
  unchecked mix bounds, ambiguous `volume`, `doctor()` dict.
- **Guard:** pydantic models + `Literal`/enums; the ruff gate keeps
  `# type: ignore` at 0 (an ignore is the tell a type is wrong).

### 5. Copy-paste drift across similar code

Agents don't search, so they re-implement - and the copies diverge into bugs.

```python
# BAD - two note parsers that disagree on "C4"/floats/unicode sharps
# notes.parse_key   accepts ♯♭ and floats
# schemas._resolve_key  rejects both  -> same string parses here, raises there

# GOOD - one canonical function, imported everywhere
from .notes import parse_key   # the single source of truth
```

- **Ours:** 2 note parsers (drifted), `_now()` ×6, `FL_APP` path ×3 (drifted
  2025 vs 2026), "is FL running" ×3, backup/restore ×3, `mode` validated 3 ways.
- **Guard:** one function per rule; diff siblings before shipping a near-copy.

### 6. Import & dependency sprawl / in-function imports

Liberal imports, deferred imports to fake "optional," `sys.path` surgery.

```python
# BAD
sys.path.insert(0, str(_REPO))
def decompile(...):
    from harness.flp_compat import parse   # deferred because top-level fails

# GOOD - fix the package boundary; import at top
from harness.flp_compat import parse
```

- **Ours:** 3 `sys.path.insert`, cross-tree deferred imports, 6 lazy
  `from . import autonomous` + `except ImportError`.
- **Guard:** ruff `PLC0415`/`TID`/`E402`; make `harness`/`flmcp_codec` one
  installed unit. Preferred libraries documented so the agent reuses them.

### 7. Hidden global state instead of injected dependencies

A function's result depends on invisible prior state; tests must monkeypatch it.

```python
# BAD
_session = None
def get_session():
    global _session
    if _session is None: _session = build()
    return _session

# GOOD - construct once at the entry point, thread it through
def write_notes(session: FLSession, ...): ...
```

- **Ours:** `_session`, `_singleton`, import-time `REG_XML`, and
  `next_command_id()` (a query-named function that read-modify-writes a counter
  file, non-atomically → duplicate command ids under concurrency).
- **Guard:** no module-level mutable globals; the tell is
  `monkeypatch.setattr(module, "internal", ...)` in tests.

### 8. Tests that test implementation, not behavior

Over-mocking (the test validates the mock), expected values copied from output,
a fake that always says yes.

```python
# BAD - passes even if write_notes is gutted
monkeypatch.setattr(server, "_try_project_write", lambda *a: {"ok": True})

# GOOD - "would this fail if the function returned a hardcoded value?"
# exercise the shipped path; against real FL for boundary behavior
notes = write_and_read_back(live_fl, [Note(key=60, start=0, length=1)])
assert notes[0].key == 60
```

- **Ours:** pervasive `monkeypatch.setattr` into internals (evidence of #7), the
  always-yes fake that hid `set_tempo`, the `_e2e_hello` attribute-smuggling hack.
- **Guard:** integration/real-FL over mocks; mocks are exceptional; the shipped
  path is the tested path (see #9); fixtures return values, they don't staple
  attributes onto objects.

### 9. Dead / bypassed abstraction kept in sync by hand

Build it, test it, then inline a parallel copy in production.

- **Ours:** `router.route_note_write` is tested but `write_notes` inlines the
  cascade - the tested path is not the shipped path.
- **Guard:** production drives the tested code, or the abstraction is deleted.

### 10. `print()` in library code / tutorial-style over-commenting

Agents pollute with prints and comments explaining basic syntax.

```python
# BAD - print in importable library code; comment restates the code
print("saving project")   # save the project

# GOOD - libraries log; CLIs print; comments say *why*, not *what*
log = logging.getLogger(__name__)
log.info("saving project to %s", path)
```

- **Ours:** `print()` in `install.py` / `make_agent_project.py` (library) - the
  smell; `print()` in `harness/*` CLIs talking to a human is fine.
- **Guard:** `logging.getLogger(__name__)` in every library module, `print` only
  in CLI entrypoints; comments explain intent.

### 11. Unsafe/unspecific exceptions, hardcoded config, magic numbers

`RuntimeError` where `TypeError` fits; secrets/paths/timeouts inline in logic.

- **Ours:** `TRY003` inline messages (retire via custom-exception classes),
  hardcoded FL paths (#5), bare timeout/`sleep` literals in `autonomous`/`live`.
- **Guard:** ruff `TRY`; named module constants; semantic exception types.

### 12. Confident refactor breaks distant callers; "done" without verifying

Renamed params / changed returns break code the agent never looked at; results
reported before tests run.

- **Guard (process):** search *all* callers before changing an interface; run the
  suite and the real-FL e2e before claiming done - never "this should pass."

## Enforcement matrix

| Pattern | Lint (automatic) | Process (review) | Test (proves it) |
|---|---|---|---|
| 1 Silent failure | - | boundary reads back | real-FL readback assertion |
| 2 Hiding exceptions | `BLE`, `SIM105` | fail closed on safety | - |
| 3 Defensive defaults | getattr ban | raise low, handle high | - |
| 4 Stringly-typed | `type:ignore`=0 | pydantic models | round-trip |
| 5 Copy-paste drift | - | diff siblings, one fn | - |
| 6 Import sprawl | `PLC0415`,`TID`,`E402` | package boundary | import works at top |
| 7 Hidden globals | - | inject deps | no `monkeypatch.setattr` internals |
| 8 Impl-not-behavior tests | - | mocks exceptional | test fails on hardcoded return |
| 9 Bypassed abstraction | - | ship the tested path | prod path is covered |
| 10 print/over-comment | print-in-lib check | comments say why | - |
| 11 Exceptions/config | `TRY` | named constants | - |
| 12 Refactor/verify | - | search callers | suite + e2e is the gate |

## Execution roadmap

The correctness and structural work is tracked in `docs/19` (Track A bugs first,
each with a real-FL assertion; Track B structure).
This document adds the patterns `docs/19` didn't yet name, folded into the same
tracks:

- Into **Track A** (correctness): patterns 1, 2, 9 - already listed.
- Into **Track B** (structure): patterns 3-7, 11 - already listed.
- **New, add to Track B:** pattern 10 (`logging.getLogger(__name__)` across
  library modules, `print` only in CLIs) and the pattern-8 test cleanup (delete
  internal monkeypatching once #7 lands; `_e2e_hello` becomes a yielded value).
- **Process, not code** (patterns 8, 12): encoded in `AGENTS.md` - search
  callers, mocks exceptional, ship the tested path, verify against real FL, never
  claim done unrun.

The order stays: correctness bugs first (they ship wrong results today), then the
structural fixes that make each pattern impossible rather than merely absent.

## Sources

- The Bugs AI Writes - Five Patterns in AI-Generated Code:
  https://dev.to/stravukarl/the-bugs-ai-writes-five-patterns-that-show-up-in-ai-generated-code-bl3
- Error-Handling Anti-Patterns (charlax/professional-programming):
  https://github.com/charlax/professional-programming/blob/master/antipatterns/error-handling-antipatterns.md
- Lint Against the Machine - Field Guide to AI Coding Agent Anti-Patterns:
  https://medium.com/@montes.makes/lint-against-the-machine-a-field-guide-to-catching-ai-coding-agent-anti-patterns-3c4ef7baeb9e
- Python Idioms and Anti-Idioms (python.org):
  https://docs.python.org/3.1/howto/doanddont.html
