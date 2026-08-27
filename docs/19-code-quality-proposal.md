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
