# R1: architecture, code health, and removing the flags

Research plan, 2026-08-30.
Pays the three debts of issue #80 under the doctrine of #54: one canonical path, capability detection instead of flags, boundaries verify by readback, net-negative line count.
Everything here is behavior-preserving except one deliberate, sequenced change: the death of `FL_MCP_BRIDGE`.

Evidence base: `docs/25-final-plan.md`, `docs/24-coverage-map.md`, `native/probe/PROBE_VERDICT_2026-08-30.md` (the live verdicts), `native/bridge/BRIDGE_FINDINGS.md`, `docs/19-code-quality-proposal.md`, issues #54/#64/#68/#80.

## 0. Summary of the plan

| Phase | What | Behavior | Net lines |
|---|---|---|---|
| P1 | Split `session.py` into a `session/` package, `FLSession` stays the facade | preserved | ~+80 (import headers) |
| P2 | One routing seam: `Capabilities` + lane table; flag becomes one input read in one place | preserved | ~0 |
| P3 | Bridge capability detection hardened: heartbeat context + protocol line + circuit breaker | preserved (flag still gates) | ~+60 |
| P4 | Delete `FL_MCP_BRIDGE`; router promotes the bridge automatically when capable | changed, live-verified | ~-40 |
| P5 | Retire the E402 baseline (package-boundary fix, docs/19 phase 2) | preserved | ~-15 |
| P6 | Delete `native/probe` source/tests/scripts (keep verdict + evidence) | preserved | ~-4,650 |

Net impact: roughly **-4,500 lines**, dominated by the probe deletion; the production tree ends slightly smaller than today even before P6.

## 1. Decompose session.py (931 lines)

### What it is today

`mcp-server/src/fl_mcp/session.py` owns six unrelated concerns: wire liveness (hello/ping/port-recycle), FL process lifecycle (SIGKILL reload, settle gate), note-write routing (file lane, flagged bridge lane), live control ops (tempo, levels, plugin params, transport), reads (snapshot, channel_names, read_notes), and the doctor report.
It is the only production file over the 800-line doctrine limit (#80 item 1).

### The seam that must not move

The public seam is two names: `from .session import FLSession, create_session` (used by `sdk.py:18`, `server.py`, and every test).
Turning `session.py` into a `session/` **package** keeps that import path byte-identical, so the SDK, the server, and the MCP tools do not change at all.

### Target layout

```
mcp-server/src/fl_mcp/session/
├── __init__.py   (~25)  re-exports: FLSession, create_session, NotePlaneUnavailable,
│                        WriteRejected, and the constants tests import. THE seam.
├── facade.py     (~170) class FLSession (thin delegation), create_session(), _project_key().
├── link.py       (~210) class ControlLink: owns control wire + factory + recycle clock.
│                        hello(), reconnect(), settle(), command(op), request(op, args, timeout).
├── lifecycle.py  (~90)  reload_fl(project, link): SIGKILL + open retries + settle.
├── detect.py     (~110) Capabilities, detect(), and the breaker evidence. See section 2/3.
├── notes.py      (~280) write_notes/clear_notes/read_notes; FileLane + BridgeLane
│                        (_bridge_send/_bridge_write/_bridge_clear/_bridge_confirmed,
│                        _spool_ppq, _heartbeat_age); _record() write-through.
├── controls.py   (~210) set_tempo, set_channel_levels, plugin_params, set_plugin_param,
│                        _plugin_read; play/stop/undo delegate to link.command().
└── state.py      (~180) snapshot(), channel_names(), doctor().
```

What moves where, exactly:

- `_hello`, `_reconnect_wire`, `_control_command`, `_settle`, and the HELLO/PING/RECYCLE/LIVE-timeout constants -> `link.py` as `ControlLink`.
  The class owns `control: MidiControl | None`, `_control_factory`, `_last_recycle`.
  `_hello()` stays THE one connectivity probe (its docstring invariant survives verbatim); everything else asks the link, never the raw wire.
- `_reload_fl` and the RELAUNCH/FL_STOP/FL_OPEN constants -> `lifecycle.py` as a free function `reload_fl(project, link)`.
  It stays the quarantined UGLY(U5); the quarantine comment moves with it.
- `write_notes`, `clear_notes`, `read_notes`, the whole `_bridge_*` family, `_heartbeat_age`, `_spool_ppq`, `_record`, `NO_COMMAND_ID`, and the BRIDGE constants -> `notes.py`.
- `set_tempo`, `set_channel_levels`, plugin params, transport -> `controls.py`.
- `snapshot`, `channel_names`, `doctor`, `PYSCRIPT_NAME`, `_SETUP_FIX` -> `state.py`.
- `NotePlaneUnavailable` and `WriteRejected` -> `fl_mcp/errors.py` (their natural home; used by both notes and controls; avoids any intra-package import cycle).
  Re-exported from `session/__init__.py` so existing `from fl_mcp.session import WriteRejected` call sites and tests keep working.

`FLSession` in `facade.py` keeps the exact same method list; each body becomes a one-line delegation (`return notes.write_notes(self, target, notes, mode)` style, or bound helpers on composed objects).
It holds `link: ControlLink`, `spool`, `store`, `project`, and exposes `control` as a property forwarding to `link.control` because tests and `server.py:53` touch `session.control` directly.

### How tests follow

`test_session.py` (809 lines, itself near the limit) splits along the same seams: `test_session_notes.py`, `test_session_controls.py`, `test_session_state.py`, `test_session_link.py`.
`test_session_bridge.py` stays and later becomes the router/capability suite (section 2).
The behavior-preserving proof is that **no assertion changes**: the tests drive the `FLSession` facade, so the only mechanical edits are monkeypatch targets (`fl_mcp.session.flp` becomes `fl_mcp.session.notes.flp`, `fl_mcp.session.fl_process_running` becomes `fl_mcp.session.link.fl_process_running`, and so on).
Gate: full local suite green before and after with the same test count (737+8 today per #80).

## 2. Kill FL_MCP_BRIDGE: automatic capability detection

### The precedent already in the codebase

The control wire has no flag.
`_hello()` detects: sticky hello + ping confirm + hot-plug port recycle, fail-closed, rate-limited.
The bridge lane gets the same treatment; the flag (`ENV_BRIDGE`, `session.py:106`) was staging for a live verdict that has now been delivered (`PROBE_VERDICT_2026-08-30.md`, `BRIDGE_FINDINGS.md` live write verdict, docs/24 rows 17-19).

### Detection signals (all fail-closed, no human input)

1. **Heartbeat freshness** (exists today): `bridge_heartbeat.txt` `unix_time=` age <= `BRIDGE_HEARTBEAT_FRESH_SECONDS` (15s).
   The bridge rewrites it every throttled tick, so a stale file means FL quit or the plugin is unloaded.
   Missing/unreadable parses as age `inf` (the existing `_heartbeat_age` contract): the gate fails closed.
2. **Protocol line** (new, one line in the bridge's tick writer): `proto=2` in the heartbeat.
   The server refuses a heartbeat whose protocol it does not speak, exactly analogous to the wire's dropped-frames codec check in `doctor()`.
   This is what "plugin loaded AND compatible" means; a hello file is not needed because the heartbeat IS the recurring hello.
3. **Live context** (new, two lines in the same tick writer): `pat=<n>` and `chan=<n>`, the bridge's view of the current pattern and selected channel, read from the ~0ms getters the probe verified (verdict Q4: full poll ~0ms against an 8.7ms tick).
   This is the signal that makes channel-honoring decidable (below).

Detection is one pure function, `router.detect(link, spool, project) -> Capabilities`, computed per operation.
No process state beyond the circuit breaker timestamp; no environment variable; nothing a human flips.

### What the bridge may be routed (probe-verdict-bounded)

- **PatNum is honored live** (verdict Q2): the bridge writes to any pattern, with the visible `SetCurrentPattern` side effect the session owns.
- **ChanNum is ignored** (verdict Q2): notes land in the currently selected channel; no channel object is reachable via any dispatcher id.

So the bridge lane is eligible for a note write **iff** bridge capability is present AND the write's channel constraint is verifiably satisfied: the fresh heartbeat's `chan=` equals `target.channel`.
Otherwise the file lane applies (it honors both coordinates by construction).
When R2's select-then-write lands live channel selection, `BridgeLane.write` selects first and the eligibility predicate widens; the router table (section 3) does not change.
This is the "always the best way" directive made mechanical: the best lane is chosen per call from evidence, and the evidence is the probe verdict encoded as an eligibility predicate, not a hope.

### Fallback ordering and the circuit breaker

- Lane choice happens **before** the write, from `Capabilities`.
- A lane failure mid-write **raises**; it never silently falls through to another lane.
  A bridge timeout is ambiguous (the command may still apply on a later tick), so retrying the same notes on the file lane inside the same call could double-apply.
  This preserves the existing "never a maybe" contract (`session.py` docstrings, the levels no-fallthrough test).
- The breaker: a bridge refusal/timeout records a monotonic timestamp in the router; `detect()` reports the bridge absent until a heartbeat **newer than the failure** appears.
  Subsequent calls route to the file lane automatically; the agent retries the failed call and it lands there.
  This is the minimum hardening docs/25 path item 1 demands before promotion.
  The out-of-process bulkhead (bridge crash takes all of FL down, docs/25 limitations table) is orthogonal durability work; it is flagged to R3 and does not gate flag removal, because the flag never protected against it either (a human with the flag on had zero extra safety).

### Receipts stay readback-not-echo, per lane

- `WriteReceipt.backend` names the lane that applied (`"project-write"` / `"bridge"`).
- File lane: `flp.write_notes` verifies by re-reading the bytes it wrote; `confirmed_notes` is that readback.
- Bridge lane: `confirmed_notes` comes from `_bridge_confirmed`'s file readback, **never** the bridge's `notes_written` echo (`FHD_AddNotesToPR` returns nothing, so the echo is only what was sent; verdict Q1: no live note readback exists, so the file is the only read of record).
  The documented lag (the file trails the live session until FL saves) is kept explicit in the receipt docstring; closing it via observer-triggered save-then-readback is R2's plan, and the receipt shape already accommodates it.
- Optional micro-addition (schemas.py): `WriteReceipt.confirmed_via: Literal["file"]` today, so a future live readback source is a visible schema change rather than a silent semantics change.

### What gets deleted with the flag

`ENV_BRIDGE`, `_bridge_ready()`, the "staged behind a double opt-in" comment block, and the flag-off test (`test_flag_off_routes_project_write_even_with_fresh_heartbeat`) which inverts into `test_fresh_heartbeat_and_matching_channel_routes_bridge`.
docs/24 line 46 ("Bridge is OFF by default") updates to name the capability predicate instead.

### Promotion gate (P4 only)

One supervised live window per the standing live-verification rules: fresh-heartbeat + channel-match write lands via bridge with no reload; stale-heartbeat write lands via file; mid-window FL kill flips routing to file on the next call with no hang.
This is the same shape as the probe verdict window and is runnable locally (section 6).

## 3. One routing seam for all three backends

### The abstraction

```python
# session/detect.py
class OpClass(Enum):
    NOTE_WRITE = auto()      # eligibility predicate decides bridge vs file per call
    NOTE_READ = auto()
    TEMPO_LEVELS = auto()
    PLUGIN_PARAMS = auto()
    TRANSPORT = auto()
    SNAPSHOT = auto()

@dataclass(frozen=True)
class Capabilities:
    hello: HelloEvent | None       # wire live (ControlLink.hello(): sticky hello + ping)
    bridge: BridgeState | None     # None unless heartbeat fresh + proto match + breaker closed
    project: Path | None           # file lane available

ROUTES: dict[OpClass, tuple[Lane, ...]] = {
    OpClass.NOTE_WRITE:    (BRIDGE, FILE),   # BRIDGE eligible only if chan constraint satisfiable
    OpClass.NOTE_READ:     (STORE, FILE),    # verdict Q1: no live note readback exists
    OpClass.TEMPO_LEVELS:  (WIRE, FILE),
    OpClass.PLUGIN_PARAMS: (WIRE,),          # plugin state has no honest file representation
    OpClass.TRANSPORT:     (WIRE,),
    OpClass.SNAPSHOT:      (WIRE, FILE),
}

def pick(op: OpClass, caps: Capabilities, target: Target | None = None) -> Lane:
    """First lane in ROUTES[op] whose capability is present (and, for NOTE_WRITE,
    whose eligibility predicate holds). Raises NotePlaneUnavailable with the
    op-class fix string when none is."""
```

### Why a table and not per-op if/else

Today the fallback ordering is re-stated inside every method (`write_notes`, `set_tempo`, `set_channel_levels`, `snapshot` each hand-roll `if self._hello() ... elif self.project ... else raise`), which is exactly how the flag metastasized into two call sites (`write_notes` and `clear_notes` each check `_bridge_ready()`).
With the table, the invariant is stated once: **capability -> lane, in declared order, fail with a named fix**.
Adding a lane (the UDP bridge of docs/22, an observer-fed live-read lane, a future automation-write lane) is one `Lane` implementation plus one table entry; no existing method changes.

`Lane` is a small protocol: `write/clear` for note lanes, `apply/read` for control lanes; each lane owns its own readback so receipts cannot degrade to echoes when a new lane is added.
The doctor consumes the same `Capabilities` object, so "connected" cannot drift between the router and the report (the `_hello()` single-probe rule, generalized).

### Sequencing note

P2 lands the table with the flag still present: `detect()` reads `ENV_BRIDGE` as one input, in one place.
That makes P2 purely behavior-preserving (existing flag tests pass unchanged), and P4 (killing the flag) becomes a one-line deletion in `detect()` plus test inversions, reviewable in isolation.

## 4. Delete the throwaway probe (native/probe)

Doctrine #54/#68: the probe's job ended at the verdict.
Measured today: **4,634 LOC** of source/tests/scripts (larger than #80's ~2,600 estimate), plus two markdown files, plus 56KB of evidence.

### Keep

- `native/probe/PROBE_VERDICT_2026-08-30.md` - the durable record; add one line naming the deletion PR and the resurrection tag.
- `native/probe/evidence-2026-08-30/` - the raw evidence the verdict cites.
- A git tag `probe-2026-08-30` on the pre-deletion commit, so re-running on a future FL build is `git checkout probe-2026-08-30 -- native/probe` (the verdict's "How this was run" then applies verbatim).

### Lift before deleting (the absorption step)

- `src/probe_safety.h` (199 lines): the **static ledger of live-fatal dispatcher ids** (94 crashes FL, 99 hangs the tick thread, verdict "Unsafe dispatcher ids"; promoted to static in PR #79).
  This is not probe-specific knowledge; it is an FL-ABI fact any future dispatcher caller (the observer, a write-side probe) must consult.
  Move it to `native/abi/fl_dispatcher_safety.h` beside `fl_host_abi.h`, and port the one guarantee test (the fresh-spool skip coverage of `TestSweepSkipsLiveConfirmedFatalsOnAFreshSpool`) into the bridge's test suite as a static ledger-contents check.
- `make_fst.py` (212 lines): only if the bridge's install path still needs a plugin-DB entry minter (R2 to confirm; `native/bridge/tools` may already cover it).
  If the bridge installer has its own, delete with the rest; the byte-exact format knowledge is recorded outside the code.

### Delete

`src/` (2,432 LOC), `tests/` (1,002), `run_probe.sh` (243), `analyze_probe.py` (323), `stamp_probe_project.py` (422), `make_fst.py` (212, unless lifted), `RUNBOOK_PROBE.md`, `PROBE_FINDINGS.md` (fold any fact not already in the verdict into it first; one pass), and the `probe` + `probe_core_test` targets in `native/CMakeLists.txt` (~40 lines).

### Precondition (dependency: R2, @flmcp-r2-ship)

The observer and select-then-write plans must (a) cite `PROBE_VERDICT_2026-08-30.md` for every capability claim they build on, and (b) confirm they need nothing from the probe source beyond the lifted ledger (e.g. the event-listener pass structure in `probe_core.cpp`, which the observer will reimplement on the bridge's own dispatcher hook).
P6 merges only after R2's docs are in.

## 5. Retire the E402 baseline (docs/19 phase 2)

The ignore lives at `mcp-server/pyproject.toml:39` with the removal already promised in its comment.
Exactly four sites depend on it:

| Site | Cause | Fix |
|---|---|---|
| `harness/doctor.py:14` | `sys.path.insert` to reach `fl_mcp` before import | delete the insert; `fl_mcp` is importable because the merge gate already installs both packages editable (`uv pip install -e . -e mcp-server`) |
| `harness/e2e.py:22` | same | same |
| `fl-scripts/controller/tests/test_codec_sync.py:20` | path setup before `import flmcp_codec` | move path setup to a new `fl-scripts/controller/tests/conftest.py`; delete the noqa |
| `fl-scripts/controller/tests/test_framing.py:16` | path setup before `import device_FLMCP` | same conftest; delete the noqa |

The package boundary is already half-fixed: the root `pyproject.toml` installs `harness` and dev-paths `fl-scripts/controller` (so `flmcp_codec` imports normally in an editable venv).
What remains is deleting the now-redundant inserts and giving the controller tests a conftest, because `device_FLMCP.py` is deliberately a flat FL-side script (frozen surface, not a package) and conftest is pytest's honest boundary for test-only path setup (conftest is imported before test modules are collected, so the test files' top-level imports become clean).
`device_FLMCP.py:344`'s runtime insert runs inside FL's embedded interpreter and is not an E402 site; untouched.

Then: remove `"E402"` from the ignore list, `ruff check .` clean, done.
`TRY003`/`PLC0415` stay baselined (tracked in #80; they are docs/19 phases 3-5, out of R1 scope).

## 6. The local gate (CI is dead, #80 meta)

No phase relies on CI.
Every phase is one PR whose merge gate is the local run, exactly the standing recipe:

```
./native/scripts/fetch_sdk.sh          # native prereq
uv pip install -e . -e mcp-server      # the package boundary P5 depends on
ruff check .
pytest mcp-server                       # 737 tests today; count recorded in each PR body
pytest fl-scripts/controller/tests      # 8 tests
cmake --build native/build && ctest --test-dir native/build
```

Red GitHub checks on this repo are billing infrastructure, not signal; the PR body records the local counts, and the head hash is re-verified at merge time.
Only P4 additionally needs a live FL window (its promotion gate, section 2), run under the standing live-verification rules (close FL before pytest; ports before launch).

## 7. Dependencies on other research areas

- **R2 (@flmcp-r2-ship, observer + select-then-write + ship plan):** P6 (probe deletion) waits for its verdict citations and the lift-list confirmation; live channel selection from R2 widens the bridge eligibility predicate (section 2) without touching the router table; observer-triggered save-then-readback closes the bridge receipt's documented file-lag.
- **R3 (@flmcp-r3-tier2):** the out-of-process bulkhead for the in-process bridge crash risk (docs/25 limitation "Bridge crashes ALL of FL").
  R1's breaker handles routing safety; R3 owns process-isolation durability.
  Flag removal does not wait for R3 (the flag provided no crash protection), but the two should not race on `bridge.cpp` edits; heartbeat context lines (P3) are small and land first.

## 8. Order of execution

P1 -> P2 -> P3 -> P5 (anytime after P1) -> P4 (after P3's breaker + one live window) -> P6 (after R2's docs land).
Each phase is an independently green, independently revertable PR.
The only user-visible change in the whole plan is P4: pattern-targeted note writes to the selected channel stop reloading FL and land live, automatically, with receipts that say which lane confirmed them.
