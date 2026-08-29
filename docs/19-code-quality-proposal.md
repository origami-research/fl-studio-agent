# Code-quality proposal: kill the smells at the root, then enforce

The recent audit found real problems (a masked bug in `get_notes`, `getattr`
cargo-cult, silent `except`). This is the systematic follow-up: the full
inventory, the *root causes* behind it, the fixes, and - most importantly - a
lint gate so it can't come back.

## The authoritative inventory (ruff, strict rules)

`ruff check src/ --select BLE,PLC0415,TRY,SIM,PTH,TID,F` → **90 findings**:

| Rule | Count | What it is |
|---|---|---|
| TRY003 | 45 | Long messages passed straight to `raise SomeError(f"...")` |
| PLC0415 | 30 | `import` inside a function (deferred / cross-tree) |
| SIM105 | 5 | `try/except: pass` that should be `contextlib.suppress` |
| PTH123 | 3 | `open(...)` instead of `Path.open(...)` |
| TRY300 | 2 | `return` in `try` that belongs in `else` |
| F401 | 2 | unused import |
| BLE001 | 1 | blind `except Exception` (the rest already carry a justified `# noqa`) |
| TRY004, PTH105 | 2 | wrong exception type / `os.replace` vs `Path` |

`getattr`/`hasattr` are already at **0** (fixed in the prior pass). `# type: ignore`
is down to **1** (`session.py:170`). So the mess is concentrated, not diffuse.

## The four root causes (fix these, most findings vanish)

### 1. `harness/`, `flmcp_codec`, and `fl_mcp` aren't one importable thing

This is the biggest structural smell and the source of every `sys.path.insert`
(3) plus most of the cross-tree PLC0415 imports:

```python
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))            # so we can import harness.*
...
def decompile(...):
    from harness.flp_compat import parse   # deferred, because top-level would fail
```

`decompile`, `flp_writer`, `live`, `control_midi`, `server` all do this to reach
`harness` or `flmcp_codec`, which live in sibling trees the package doesn't know
about.

**Fix:** make them a real, installed unit. Options, cheapest first:
- Add `harness` and the controller `flmcp_codec` to the package's known imports
  (a `pyproject` `[tool.setuptools] packages`/path entry, or move `harness/`
  under `mcp-server/src/` as `fl_mcp.harness`, or a namespace package).
- Then `import harness.verify` / `import flmcp_codec` just work everywhere - the
  3 `sys.path.insert`s and ~10 deferred cross-tree imports all become normal
  top-level imports.

### 2. Sibling modules imported lazily to fake "optional"

`server.py` has `from . import autonomous` **six times**, each inside a function,
usually wrapped in `try/except ImportError`. But `autonomous` is a sibling in the
same package - it is *always* importable. The laziness is theater; what's
actually optional is a *runtime* dependency (`pyflp`, a configured project),
which belongs inside `autonomous`, not at its import.

**Fix:** import `autonomous`/`live` once at top. Let their *functions* report
"unavailable" via a typed result when their runtime prerequisite is missing.
Deletes 6 deferred imports + their `except ImportError`.

### 3. Stringly-typed tool results (`dict[str, Any]` × 48)

Every tool hand-builds `{"status": "...", ...}`. Shapes drift (`applied` vs
`ok` vs `status`), the model sees an inconsistent envelope, and the one
remaining `# type: ignore` exists because `report` is `dict[str, object]` so
`.get` doesn't type.

**Fix:** a small set of pydantic **result models** with one envelope -
`Applied`, `Queued`, `Reads`, `Err` - built on the existing `schemas`. Tools
return a typed result; a single `.model_dump()` at the MCP boundary makes the
dict. One shape, validated, self-documenting, and the `type: ignore` disappears
because the report is a model, not an untyped dict.

### 4. Exceptions used for control flow / as vanilla raises

- TRY003 (45): `raise FLError(f"FL Studio not found at {path}...")` - the long
  message inline. **Fix:** give the custom exceptions their message shape (or a
  classmethod), so call sites read `raise FLNotFound(path)`.
- SIM105 (5): `try: ...; except: pass` → `with contextlib.suppress(SpecificError):`.
- BLE001 / TRY004 / TRY300: narrow to the real exception, or convert to a typed
  `Err(...)` result, and move `return` into `else`.
- Rule everywhere: **exceptions only at true boundaries** (IO, subprocess, the
  MIDI wire, the parser). The middle of the code raises and trusts callers; a
  boundary either converts to a typed result or logs+re-raises - it never
  swallows.

## Enforcement: a ruff gate (the part that makes it stay good)

A one-time scrub rots. The durable fix is a linter that fails CI on these, so a
regression is caught in the PR, not months later. Proposed `pyproject.toml`:

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "BLE", "PLC0415", "TRY", "SIM", "PTH", "TID", "RUF"]
# BLE  - no blind except        PLC0415 - no import inside functions
# TRY  - exception hygiene       SIM     - simplify (contextlib.suppress, ...)
# PTH  - pathlib over os/open    TID     - tidy imports
ignore = ["TRY003"]  # temporary: the 45 vanilla-raise messages, fixed in phase 3
```

Add `ruff check` to CI (a GitHub Action or the existing test job). Every `# noqa`
must carry a code **and** a reason, so suppressions are deliberate and reviewable.

## Phased plan (each phase a small, green PR)

1. **Enforce first, baseline the rest.** Land the ruff config + CI gate with the
   current findings baselined (`per-file-ignores` or `ignore`), and auto-fix the
   free wins (F401, SIM105, PTH). Now nothing *new* regresses. (~½ day)
2. **Package unification** (root cause 1): make `harness`/`flmcp_codec`
   importable; delete the 3 `sys.path.insert`s and cross-tree deferred imports.
   Removes ~15 PLC0415. (~1 day)
3. **Top-level sibling imports** (root cause 2): remove the 6 lazy
   `from . import autonomous` + `except ImportError`; push optionality into the
   modules. (~½ day)
4. **Typed results** (root cause 3): the result-model set + envelope; drop the
   48 ad-hoc dicts and the last `type: ignore`. Biggest readability win. (~1-2 days)
5. **Exception polish** (root cause 4): custom-exception messages (retire the
   TRY003 ignore), narrow remaining catches. (~½ day)

## The principle underneath all of it

Trust the types and let the structure carry the guarantees, so the defensive
code has nothing to defend against. `getattr`, `# type: ignore`, blind `except`,
and `sys.path` surgery are all symptoms of fighting the type system or the
package boundary - fix the boundary and the symptoms have no reason to exist.
The lint gate is what keeps that true after we look away.

---

# Round-2 deep audit: what a linter can't see

The inventory above is the *mechanical* layer - what ruff can flag.
Three reviewers then read the whole package (type design, silent failures,
state/coupling) for the structural rot underneath.
They surfaced **two root causes the first pass missed**, a cross-cutting
DRY-drift problem, and - most importantly - a set of **real correctness bugs**,
not taste nits.
This section supersedes the phased plan above with a single ordered plan.

## Two more root causes (the first pass missed these)

### 5. Hidden state instead of explicit dependencies

The owner's rule: every function takes what it needs and returns what it
produces - no invisible module state.
We violate it in five places, and each one forces a test to reach into internals
to reset or fake it:

- `server.py:55` `_session` global + `global _session` - process-wide singleton.
- `control_midi.py:203` `_singleton` global + `global _singleton`.
- `spool.py:98` `next_command_id()` - a *query-named* free function that
  read-modify-writes an on-disk counter. Not just hidden state: the RMW isn't
  atomic, so two concurrent tool calls mint **duplicate command ids** and
  correlate the wrong FL response to the wrong command (data-integrity bug).
- `install.py:26` `REG_XML = Path.home() / ...` bound at *import* time - ambient
  config; `test_install_midi.py` must monkeypatch it to test.
- `flp_compat.py:29` `_patched` global flag guarding a process-wide monkeypatch
  of a PyFLP class method (defensible as a shim, but the boolean can fall out of
  sync with the live class - check the class, not a flag).

**The tell:** the pervasive `monkeypatch.setattr(module, "internal", fake)` across
the test suite (`test_router`, `test_channels`, `test_live_backend`,
`test_server_v1`) is *evidence*, not a separate defect - those seams exist only
because the code reads module singletons instead of receiving dependencies.

**Fix (structural):** construct dependencies once at the MCP entry point (the
FastMCP lifespan) and thread a `session` / `AppContext` through the tools.
No `global`, no import-time config, no free function reaching into a counter
file. Id allocation becomes a lock-guarded method on an owned object
(`SpoolSession` already has the correct one - the v0 free function is the bug).
This one change dissolves most of the test monkeypatching too.

### 6. Boundaries that trust instead of verify

This is the direct descendant of the `set_tempo` lesson (green against the fake,
broken against real FL).
The "unsafe at current time" *crash* was fixed, but the **success path still
reports the value it requested, not the value FL confirms.**
Four boundaries claim success they never verified:

| Where | Claims | Actually |
|---|---|---|
| `flp_writer.py:write_notes` | docstring says "re-reads the saved file" | returns `len(blob)//NOTE_SIZE` (intended count), never re-parses. `set_tempo`/`set_channel_levels` in the *same file* do re-parse - `write_notes` is the gap. |
| `server.py:set_tempo` (live) | `"tempo": bpm` applied | reports the *requested* bpm; never reads `mixer.getCurrentTempo()` back, though `_hello_fields` already calls it. |
| `server.py:set_channel_mix` | one clean `"applied"` | if live volume lands but live pan fails, it falls through to a file-rewrite + FL reload that *discards* the live volume - partial edit silently blown away. |
| `MCP Write Notes.pyscript:_consumed_ids` | anti-replay ledger | on a corrupt/torn `consumed.json` it `except: pass` → treats it as empty → **replays** an already-applied `add_notes`/`clear_notes` (duplicate notes). |

**Fix (principle):** a boundary either reads back and reports the *confirmed*
state, or returns a typed result naming what actually happened and which backend
did it - it never returns the value it hoped for.
"Verify against real FL" isn't only a test rule; it's how the boundary code must
behave at runtime.
The fake can't catch these because the fake always says yes - so each fix needs a
**real-FL e2e assertion** that reads the value back.

## Cross-cutting: duplicated logic that has already drifted

Not a new category, but the audit found the duplication is no longer theoretical -
it has **drifted into a latent correctness bug**:

- **Two note-name parsers.** `notes.py:parse_key` and `schemas.py:_resolve_key`
  both do `"C4"→60`, but they disagree: `parse_key` accepts unicode `♯♭` and
  floats; `_resolve_key` rejects both. Different call sites use different ones
  (`kernel`/`theory` vs `server`/`diff`/`views`), so the *same note string parses
  in one path and raises in another.
- `_now()` reimplemented in **6** modules (`records`, `session`, `kernel`,
  `spool`, `router`, + a `schemas` lambda) - time can't be frozen in tests.
- `FL_APP` default path hardcoded in **3** places and already inconsistent:
  prod says `FL Studio 2025.app`, the e2e fixture says `2026.app` - the test
  launches a different app than the backend targets.
- "Is FL running" checked **3** ways (`pgrep -fi` vs `pgrep -f` vs `trigger`).
- Backup→write→restore copy-pasted **3×** in `autonomous.py`.
- `mode` ("merge"/"replace") re-validated **3** ways with **3** different failure
  behaviors (raise / error-dict / silently coerce to merge) despite an existing
  `Literal` type.

**Fix:** one canonical function per rule. One parser, one clock, one FL-app
resolver, one process check, one `project_backup` context manager, one
`NoteWriteMode` alias. Every one of these is net-negative LOC.

## Dead abstraction kept in sync by hand

`router.route_note_write` + the `NoteBackend` type exist and are covered by
`test_router.py` - but production `server.write_notes` **inlines the whole
cascade** and never calls it.
The tested path is not the shipped path.
**Fix:** drive `write_notes` through `route_note_write`, or delete the unused
entry point. No parallel copies kept in sync by hand.

## The full firm-type inventory (from the type reviewer)

Beyond `Note.key`, the same "union/`Any` never resolved" disease across the models
(each forces re-parsing or a `# type: ignore` downstream):

1. `Note.key: str | int` → `key: int` + `field_validator(mode="before")`.
2. `Target.pattern/channel: int | str = -1` - a magic `-1` sentinel inside a
   union; resolve names→indices once at the tool boundary, split "current" from a
   real index instead of overloading `-1`.
3. `mode: str` → shared `NoteWriteMode = Literal["merge","replace"]`.
4. `dict | None` backend returns conflate unavailable/failed/timed-out → a tagged
   `Applied | Unavailable | Rejected` result (also kills the re-probe in `_queued`).
5. `ControlRequest.args/kwargs/ret: Any` - the whole live RPC is untyped →
   per-op typed request/response (the `OP_ARGS` registry pattern, applied to the
   control plane).
6. `SpoolCommand.args: dict[str, Any]` → discriminated union keyed by `op`.
7. `ActionEntry.before/after: Any` in the durable log → narrow / per-op payload.
8. `decompile.Step.args: dict[str, Any]` → discriminated `Step` union per op.
9. `set_tempo`/`set_channel_mix` bounds hand-checked or *unchecked* (an illegal
   `volume=57.0` ships straight to FL) → pydantic arg models like `AddNotesArgs`.
10. `Channel.volume_db` vs `set_channel_mix`'s linear `volume` - two scales, one
    name → distinct types so a stray pass-through is a type error.
11. `session.doctor()` `dict[str, object]` (the last `# type: ignore`) →
    `PlaneStatus`/`DoctorReport` models.

## The unified plan (ordered: correctness first, then structure)

Correctness bugs are not "phase 4 taste" - they ship wrong results today.
Fix them first, each with a real-FL assertion that would have caught it.

**Track A - correctness (do first, each gets a real-FL e2e test):**

1. **Unify the note parser** (H3): one function; delete the duplicate. Highest
   correctness risk, ~1 function. *(net-negative)*
2. **`next_command_id` race** (H1): move id allocation onto a lock-guarded owned
   object; no free function touching the counter file. Log persist failures.
3. **`flp_writer.write_notes` re-read**: re-parse after save, return the count
   *actually persisted*, matching its siblings.
4. **`set_tempo` live readback**: report `mixer.getCurrentTempo()`, not the request.
5. **`set_channel_mix` all-or-nothing**: resolve both fields, commit through one
   backend, name which backend applied - no partial-then-discarded.
6. **`consumed_ids` fail-closed**: a corrupt ledger surfaces and refuses to
   consume, never silently replays.
7. **`kernel.flush`**: make code and docstring agree on rejection handling.

**Track B - structure (makes the bad code impossible, mostly net-negative):**

8. **Package unification** (root cause 1, from the first plan): kill the 3
   `sys.path.insert`s + cross-tree deferred imports.
9. **Dependency injection** (root cause 5): construct `session`/context at the
   entry point; delete `_session`, `_singleton`, import-time `REG_XML`; dissolve
   the test monkeypatching. Top-level sibling imports (old root cause 2).
10. **Firm types** (root causes 3 + the 11-item inventory): `Note.key: int`,
    `Target` boundary resolution, `NoteWriteMode`, tagged backend results, typed
    RPC/Spool/Step/Action/doctor models. Drops the ~48 `dict[str,Any]` and the
    last `# type: ignore`.
11. **DRY consolidation**: one clock, one FL-app resolver, one process check, one
    `project_backup`. Inject the clock so time is freezable.
12. **Wire or delete the router** (H4): production drives `route_note_write`;
    `write_notes` stops being a 130-line god function.
13. **Exception polish** (old root cause 4): custom-exception messages (retire
    the TRY003 ignore), narrow the `list_channels` catch, add rate-limited
    telemetry to the silent-drop paths (`read_json`, `_on_message`) so corruption
    is diagnosable instead of presenting as a generic timeout.

**Enforcement additions (ruff can't see these):**

- A test must exercise the **shipped** path, not a parallel abstraction (H4 is
  how that rot starts).
- Live-tier ops **read back** the value they set; the e2e suite asserts the
  confirmed value, not the requested one.
- The real-FL e2e run is the gate for Track A - the fake is insufficient by
  construction.
