"""The discovery pipeline: the generic Pass runner, the aligner, the passes.

Everything here is transport-free: a FakeTransport hands the runner canned wp_*
results, so the pipeline is tested end to end without FL. The three checks the
lane brief names: the runner folds mock responses into the profile, map_align
classifies a synthetic table (matched/moved/new), and the sweep consumes only
the unknowns the aligner emits.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fl_mcp.core.discovery import (
    Command,
    FileLedger,
    NullLedger,
    discover,
)
from fl_mcp.passes import (
    Getters,
    MapAlign,
    Plugins,
    Sweep,
    Target,
    Timing,
    action_trace,
    classify,
    classify_plugins,
    unknown_ids,
)
from fl_mcp.passes.map_align import DOCUMENTED_MAX_ID
from fl_mcp.profile import BUILTIN_FALLBACK, GetterFact, UnsafeFact

# --- fakes -------------------------------------------------------------------


@dataclass(frozen=True)
class FakeResponse:
    ok: bool
    result: dict[str, Any]


@dataclass
class FakeTransport:
    """Returns a canned result per op and records every command it was asked to send."""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    sent: list[Command] = field(default_factory=list)

    def request(self, cmd: Command, timeout: float) -> FakeResponse:
        self.sent.append(cmd)
        return FakeResponse(ok=True, result=self.results.get(cmd.op, {}))


def _profile_with_dispatcher(**overrides: Any):
    """BUILTIN_FALLBACK with only its dispatcher facts overridden - a real profile
    shape, a synthetic jump table."""
    dispatcher = dataclasses.replace(BUILTIN_FALLBACK.dispatcher, **overrides)
    return dataclasses.replace(BUILTIN_FALLBACK, dispatcher=dispatcher)


# --- map_align: classify a synthetic table -----------------------------------


def test_classify_partitions_a_synthetic_table() -> None:
    # A tiny handled surface: documented 0..DOCUMENTED_MAX_ID exist; ids 5 and 6
    # went inert (handlers disappeared); ids 90 and 91 are new undocumented
    # handlers; 92 is an undocumented but inert (empty) slot.
    profile = _profile_with_dispatcher(handled_max_id=91, inert_in_range=(5, 6, 92))
    align = classify(profile)

    assert 5 in align.moved and 6 in align.moved  # documented, now inert
    assert set(align.new) == {88, 89, 90, 91}  # undocumented, handled
    assert 92 not in align.new  # undocumented AND inert = empty slot, ignored
    assert all(i <= DOCUMENTED_MAX_ID for i in align.matched)
    assert 0 in align.matched and 4 in align.matched


def test_classify_names_findings_from_the_sdk_map() -> None:
    align = classify(_profile_with_dispatcher(handled_max_id=91, inert_in_range=(39,)))
    assert align.name(39) == "FHD_GetTimeMul"  # a documented, now-inert id, named
    assert align.name(90).startswith("undocumented_")


# --- unknown_ids: the shrunk sweep input -------------------------------------


def test_unknown_ids_excludes_known_unsafe_and_getters() -> None:
    profile = _profile_with_dispatcher(
        handled_max_id=95,
        inert_in_range=(),
        unsafe={94: UnsafeFact(hazard="crashes_host", evidence="x")},
    )
    # new = 88..95; 94 is a known fatal -> excluded; the getters (36/37/... all
    # documented) never enter the new set.
    assert unknown_ids(profile) == [88, 89, 90, 91, 92, 93, 95]


def test_unknown_ids_empty_when_all_new_ids_are_accounted_for() -> None:
    # handled surface stops at the documented bound: nothing undocumented is
    # handled, so there is nothing to sweep - a converged build.
    profile = _profile_with_dispatcher(handled_max_id=DOCUMENTED_MAX_ID, inert_in_range=())
    assert unknown_ids(profile) == []


# --- plugins: static candidate map, never an executable probe ----------------


def test_plugin_candidates_classify_only_nonfatal_undocumented_handlers() -> None:
    profile = _profile_with_dispatcher(
        handled_max_id=109,
        inert_in_range=(102,),
        unsafe={94: UnsafeFact("crashes_host", "x"), 99: UnsafeFact("hangs_host", "x")},
    )
    candidates = classify_plugins(profile)

    for finding in (candidates.add_instrument, candidates.add_effect, candidates.load_preset):
        assert finding.matched == () and finding.moved == ()
        assert finding.new == (88, 89, 90, 91, 92, 93, 95, 96, 97, 98, 100, 101, 103, 104, 105, 107, 108, 109)
        assert not set(finding.new) & {94, 99, 106}


def test_plugin_pass_is_static_and_never_emits_unproven_dispatcher_ids() -> None:
    profile = _profile_with_dispatcher(handled_max_id=109, inert_in_range=())
    transport = FakeTransport()

    out = discover([Plugins()], transport, profile)

    assert transport.sent == []
    assert out == profile


def test_plugin_action_trace_is_an_observation_only_orchestrator_handoff() -> None:
    profile = _profile_with_dispatcher(
        handled_max_id=109,
        inert_in_range=(102,),
        unsafe={95: UnsafeFact("crashes_host", "observed")},
    )

    trace = action_trace(profile)

    assert trace.executor == "orchestrator-only"
    assert trace.dispatch_commands == ()
    assert {94, 95, 99, 106} <= set(trace.forbidden_ids)
    assert [expectation.operation for expectation in trace.expectations] == [
        "add_instrument",
        "add_effect",
        "load_preset",
    ]
    assert all("hook enter/return trace" in expectation.readback for expectation in trace.expectations)
    assert all("saved-project diff" in expectation.readback for expectation in trace.expectations)


# --- sweep consumes only the unknowns ----------------------------------------


def test_sweep_commands_carry_exactly_the_unknowns() -> None:
    profile = _profile_with_dispatcher(
        handled_max_id=93, inert_in_range=(), unsafe={92: UnsafeFact("hangs_host", "x")}
    )
    (cmd,) = Sweep().commands(profile)
    assert cmd.op == "wp_sweep"
    assert cmd.args["ids"] == unknown_ids(profile) == [88, 89, 90, 91, 93]


def test_sweep_issues_no_command_when_nothing_is_unknown() -> None:
    profile = _profile_with_dispatcher(handled_max_id=DOCUMENTED_MAX_ID, inert_in_range=())
    assert Sweep().commands(profile) == []


# --- the runner folds mock responses into the profile ------------------------


def test_getters_fold_records_confirmed_getters() -> None:
    transport = FakeTransport(
        results={
            "wp_getters": {
                "results": [
                    {"id": 36, "getter": "FHD_GetMixingTime", "call": "buffer_in_value", "ret": 1},
                    {"id": 76, "getter": "FHD_GetContextInfo", "call": "index_only", "ret": 3},
                    {"id": 39, "getter": "FHD_GetTimeMul", "skipped": "quarantined"},
                ]
            }
        }
    )
    out = discover([Getters()], transport, BUILTIN_FALLBACK)
    by_id = {g.id: g for g in out.getters}
    assert by_id[36].convention == "guarded_buffer"
    assert by_id[76].convention == "index_only"
    assert 39 not in by_id  # a skipped getter is not recorded as confirmed


def test_timing_fold_updates_medians_on_matching_getters() -> None:
    start = dataclasses.replace(
        BUILTIN_FALLBACK,
        getters=(GetterFact(36, "FHD_GetMixingTime", "guarded_buffer", 0),),
    )
    transport = FakeTransport(results={"wp_timing": {"getters": [{"id": 36, "median_ns": 4200}]}})
    out = discover([Timing()], transport, start)
    assert out.getters[0].median_ns == 4200


def test_sweep_fold_promotes_learned_fatals_into_the_profile() -> None:
    # The Bridge reports id 106 skipped (native ledger, hangs) and id 95 skipped
    # (dynamic quarantine). Both must land in dispatcher.unsafe - a new build's
    # crashers flow into the profile as data, no C++ edit.
    profile = _profile_with_dispatcher(handled_max_id=109, inert_in_range=(102,), unsafe={})
    transport = FakeTransport(
        results={
            "wp_sweep": {
                "results": [
                    {"id": 88, "ret": 0, "ret_class": ""},
                    {"id": 95, "skipped": "quarantined"},
                    {"id": 106, "skipped": "hangs_host"},
                ]
            }
        }
    )
    out = discover([Sweep()], transport, profile)
    assert out.dispatcher.unsafe[106].hazard == "hangs_host"
    assert out.dispatcher.unsafe[95].hazard == "crashes_host"  # dynamic quarantine -> conservative
    assert 88 not in out.dispatcher.unsafe  # a benign survivor is not quarantined


def test_static_pass_folds_nothing_and_issues_nothing() -> None:
    transport = FakeTransport()
    out = discover([MapAlign(), Target()], transport, BUILTIN_FALLBACK)
    # MapAlign issues no command; Target plants one wp_target but folds no fact.
    assert [c.op for c in transport.sent] == ["wp_target"]
    assert out == BUILTIN_FALLBACK


# --- crash-resume via the ledger ---------------------------------------------


def test_default_ledger_is_null_and_three_arg_call_works() -> None:
    transport = FakeTransport(results={"wp_getters": {"results": []}})
    # docs/30 Surface 4 signature: discover(passes, transport, profile).
    out = discover([Getters()], transport, BUILTIN_FALLBACK)
    assert out is not None
    assert len(transport.sent) == 1


def test_file_ledger_replays_finished_commands_on_resume(tmp_path: Path) -> None:
    ledger_path = tmp_path / "led.json"
    results = {"wp_getters": {"results": [{"id": 36, "getter": "g", "call": "index_only", "ret": 1}]}}

    first = FakeTransport(results=results)
    discover([Getters()], first, BUILTIN_FALLBACK, ledger=FileLedger(ledger_path))
    assert len(first.sent) == 1  # issued once

    # A fresh run with the SAME ledger file must replay, not re-issue.
    second = FakeTransport(results=results)
    out = discover([Getters()], second, BUILTIN_FALLBACK, ledger=FileLedger(ledger_path))
    assert second.sent == []  # nothing re-sent
    assert {g.id for g in out.getters} == {36}  # yet the recorded result still folds


def test_null_ledger_never_skips() -> None:
    led = NullLedger()
    assert led.get("getters/0") is None
    led.record("getters/0", FakeResponse(True, {}))
    assert led.get("getters/0") is None


# --- the CLI's report + non-live run -----------------------------------------


def test_diff_report_names_the_field_that_moved() -> None:
    from fl_mcp.discover import diff_report

    moved = _profile_with_dispatcher(handled_max_id=110)
    report = diff_report(BUILTIN_FALLBACK, moved)
    assert "dispatcher.handled_max_id: 109 -> 110" in report
    assert diff_report(BUILTIN_FALLBACK, BUILTIN_FALLBACK) == "diff vs baseline: no change"


def test_alignment_report_states_matched_moved_new_and_the_sweep_input() -> None:
    from fl_mcp.discover import alignment_report

    report = alignment_report(_profile_with_dispatcher(handled_max_id=91, inert_in_range=(39,)))
    assert "matched" in report and "moved" in report and "new" in report
    assert "FHD_GetTimeMul" in report  # the moved id is named from the SDK map


def test_plugin_report_names_all_operations_and_the_non_executable_boundary() -> None:
    from fl_mcp.discover import plugin_report

    report = plugin_report(_profile_with_dispatcher(handled_max_id=91, inert_in_range=()))
    assert "add instrument" in report and "add effect" in report and "load preset" in report
    assert "unproven; static decode only" in report


def test_plugin_trace_report_names_observation_and_forbidden_ids() -> None:
    from fl_mcp.discover import plugin_trace_report

    report = plugin_trace_report(_profile_with_dispatcher(handled_max_id=91, inert_in_range=()))
    assert "orchestrator-only; observation, never dispatch" in report
    assert "forbidden ids: [94, 99, 106]" in report


def test_run_without_dylib_or_live_needs_no_transport() -> None:
    from fl_mcp.discover import run

    # dylib=None skips the scrape; live=False skips the passes; so no transport is
    # touched - the static alignment report is still printed as a side effect.
    out = run(BUILTIN_FALLBACK, None, live=False, ledger_dir=None)
    assert out == BUILTIN_FALLBACK
