// The static fatal-id ledger - the single source of truth for which host
// dispatcher ids automated code must never call blind, and why.
//
// Lineage: Spike 1.5 dispatcher probe (v1 ledger, recovered from git history)
// -> git 2ad0d74: native/probe/src/probe_safety.h (made load-bearing for the probe's
// sweeps) -> here, native/abi/, when the probe's passes were folded into the
// Bridge as wp_* discovery ops and native/probe/ was deleted (docs/26 Phase
// B1/C3). The live-confirmed fatals 94/99 were paid for exactly once
// (PROBE_VERDICT_2026-08-30.md); keeping them in a STATIC ledger is what makes
// them stay paid on any machine, any spool, any operator. This file must
// therefore outlive every diagnostic that ever seeds it.
//
// This is bulkhead invariant #1 (docs/research/r2 s2.2): the Bridge's normal
// operation issues only live-proven ids (FHD_AddNotesToPR, FHD_SetDirty, the
// confirmed getters), and the wp_* discovery ops consult IsUnsafe() /
// IsDocumented() before every dispatcher call, so a dangerous id is skipped by
// construction even if an operator asks for it.
//
// Categories:
//   NULL_DEREF  - dereferences Value as a pointer to a struct it writes into;
//                 Value=0 is a null-pointer store (crash). The wp_getters op
//                 reaches these safely with a caller-allocated guarded buffer.
//   MODAL_UI    - opens a modal dialog/picker/menu. Never crashes, but freezes
//                 an automated run waiting for a human to dismiss it (and
//                 OnIdle-driven ticks freeze under modals). Never unattended.
//   PTR_DEREF   - dereferences Index or Value as a string/array pointer;
//                 Index=0/Value=0 or a small packed int is an invalid read.
//   EXTERNAL    - launches an external application or tool.
//   MUTATES     - changes project/host state; no place in a read-only sweep.
//   CRASHES/HANGS - LIVE-CONFIRMED fatal on FL 2026. The only entries that
//                 come from a run rather than a decode.
#pragma once

#include <cstdint>

// The per-FL-version facts (kHandledMaxId, the fatal ids, the default PPQ,
// the struct-assert wall) have ONE home: L2's capability profile, rendered
// into native/generated/fl_profile_generated.h by gen_layout. Until that
// header is generated the hand-verified FL 2026 constants below stand in, so
// this tree builds in isolation; when it is present, the generated values are
// consumed and checked for agreement with the curated hazard table.
#if __has_include("fl_profile_generated.h")
#include "fl_profile_generated.h"
#endif

namespace fl_dispatch {

enum class Hazard {
  kNullDeref,
  kModalUI,
  kPtrDeref,
  kExternal,
  kMutates,
  kCrashesHost,
  kHangsHost,
};

struct UnsafeId {
  int id;
  Hazard hazard;
  const char *name;
  const char *why;
};

// The maximum documented dispatcher id (fp_plugclass.h documents FHD_* 0-87).
// A blind sweep skips this whole range by construction (see IsDocumented) -
// the documented ids carry no new information for a sweep and many have side
// effects, so this is the invariant that keeps a sweep read-only and safe
// over ANY range, WITHOUT relying on the ledger below enumerating every
// side-effecting documented id.
inline constexpr int kDocumentedMaxId = 87;

// The highest id FL 2026 actually handles, read off FL's own dispatcher.
//
// `TFruityHost.Dispatcher` (VMT+200 = 0x427bc0 in FLEngine_x64.dylib, FL 2026
// 26.1.5 build 5384, arm64 slice) opens with `cmp x0, #0x6d; b.hi <default>`
// and then indexes a 110-entry jump table at 0x42da00. So ids 0..109 are the
// entire handled surface: every id from 110 up takes the default branch,
// returning zero and touching nothing. Unhandled ids INSIDE the range (same
// default): 39 (FHD_GetTimeMul), 62, 63, 64, 102.
// See native/probe/PROBE_FINDINGS.md, "FL 2026's dispatcher surface"; source deleted.
#ifdef FLMCP_PROFILE_GENERATED
inline constexpr int kHandledMaxId = fl_profile::kHandledMaxId;
#else
inline constexpr int kHandledMaxId = 109;
#endif

// Is this id past FL 2026's handled surface? Such an id is inert by decode -
// a sweep may still call it (that is the control), but nothing should happen.
inline constexpr bool IsBeyondDispatcher(int id) { return id > kHandledMaxId; }

// The ledger. The first block is the v1 set, VERBATIM (id 17; 15/57/60;
// 19/41/43/74/85). The second block extends it with the documented ids whose
// hazard is worth naming explicitly - the memory-unsafe (pointer deref), the
// modal, the external-launch, and the buffer-out getters wp_getters reaches
// safely. This list names the hazard CATEGORIES; it is NOT an exhaustive
// catalogue of every side-effecting documented id, and does not need to be:
// IsDocumented() makes a sweep skip the whole 0-87 range regardless. The
// ledger's role is (a) to carry the v1 hazards verbatim, (b) to annotate skip
// lines with a concrete reason, and (c) to gate any curated candidate list
// that could, in principle, name a documented id.
inline constexpr UnsafeId kLedger[] = {
    // --- v1 ledger, verbatim -------------------------------------------------
    {17, Hazard::kNullDeref, "FHD_AddNotesToPR",
     "dereferences Value as TNotesParams*; Value=0 is a null store. A "
     "mock-host test confirmed the naive-host null deref. The Bridge's note "
     "executor calls this correctly, with a real chord buffer."},
    {15, Hazard::kModalUI, "FHD_ShowPlugSelector", "opens the plugin selector (modal)"},
    {57, Hazard::kModalUI, "FHD_ShowPicker", "opens the plugin/project picker (modal)"},
    {60, Hazard::kModalUI, "FHD_RenderProject", "prompts the rendering dialog (modal)"},
    {19, Hazard::kPtrDeref, "FHD_MsgBox", "dereferences Index as a 'Title|Message' string"},
    {41, Hazard::kPtrDeref, "FHD_SendSysEx", "dereferences Value as a length-prefixed byte array"},
    {43, Hazard::kPtrDeref, "FHD_LoadInChannel", "dereferences Value as a filename string"},
    {74, Hazard::kPtrDeref, "FHD_ReportError", "dereferences Index as an error string"},
    {85, Hazard::kPtrDeref, "FHD_ShowException", "dereferences Index/Value as exception strings"},

    // --- extension: the rest of the documented range a blind sweep must skip -
    // pointer-in-Value getters/setters (the time getters 36/37/38 are the ones
    // wp_getters reaches safely with a guarded buffer):
    {7, Hazard::kPtrDeref, "FHD_LocatePlugin", "dereferences Value as a filename string"},
    {10, Hazard::kPtrDeref, "FHD_SetNewName", "dereferences Value as a string"},
    {14, Hazard::kPtrDeref, "FHD_LocateDataFile", "dereferences Value as a filename string"},
    {18, Hazard::kPtrDeref, "FHD_GetParamMenuEntry", "writes through a Result pointer"},
    {28, Hazard::kPtrDeref, "FHD_PackDataFile", "dereferences Value as a filename string"},
    {35, Hazard::kPtrDeref, "FHD_SetUID", "dereferences Value as a string"},
    {36, Hazard::kNullDeref, "FHD_GetMixingTime",
     "fills a TFPTime through Value; Value=0 is a null store. Reached safely "
     "by wp_getters with a guarded buffer."},
    {37, Hazard::kNullDeref, "FHD_GetPlaybackTime",
     "fills a TFPTime through Value; wp_getters only, guarded buffer."},
    {38, Hazard::kNullDeref, "FHD_GetSelTime",
     "fills TFPTime t/t2 through Value; wp_getters only, guarded buffer."},
    {42, Hazard::kPtrDeref, "FHD_LoadAudioClip", "dereferences Value as a filename string"},
    {45, Hazard::kPtrDeref, "FHD_DebugLogMsg", "dereferences Value as a string"},
    {49, Hazard::kPtrDeref, "FHD_AddToRecent", "dereferences Value as a string"},
    {51, Hazard::kNullDeref, "FHD_GetInName", "writes a TNameColor through Value"},
    {52, Hazard::kNullDeref, "FHD_GetOutName", "writes a TNameColor through Value"},
    {66, Hazard::kNullDeref, "FHD_RenderWindowBitmap",
     "writes through a TRenderWindowBitmapInfo pointer"},
    {77, Hazard::kMutates, "FHD_SetContextInfo", "changes context info; not a getter"},
    {86, Hazard::kPtrDeref, "FHD_GetTranslationMoFile",
     "returns/handles a translation object pointer"},

    // modal UI beyond the v1 three:
    {44, Hazard::kModalUI, "FHD_ShowInBrowser", "jumps the browser to a file"},
    {53, Hazard::kModalUI, "FHD_ShowEditor", "brings up the plugin editor"},
    {55, Hazard::kModalUI, "FHD_ShowSettings", "toggles the settings panel"},
    {75, Hazard::kModalUI, "FHD_ShowStandardParamMenu", "pops up a parameter menu (modal)"},
    {83, Hazard::kModalUI, "FHD_PickVoiceColor", "shows the piano roll color picker (modal)"},

    // launches an external process:
    {70, Hazard::kExternal, "FHD_OpenTool", "launches an application from System\\Tools"},

    // --- live-confirmed fatal, 2026-08-30 window (PROBE_VERDICT_2026-08-30.md)
    // Undocumented ids, so nothing above catches them: they were found by the
    // probe sweep's crash-safety the expensive way, once. Static is the only
    // place a live-learned crasher stays learned.
    {94, Hazard::kCrashesHost, "(undocumented 94)",
     "crashes FL 2026 with an EAccessViolation when called blind "
     "(native/probe/evidence-2026-08-30/crash-id94.log)"},
    {99, Hazard::kHangsHost, "(undocumented 99)",
     "blocks the tick thread forever when called blind - no crash, no return, "
     "so the pass never advances and the window stalls"},
    {106, Hazard::kHangsHost, "(undocumented 106)",
     "wedges the bridge tick when called blind (V3 live verification, 2026-08-30: "
     "wp_sweep 88-109 skipped 94/99, FL survived, then stalled with wp_inflight "
     "stuck at id=106, no crash and no advance; recovered by killing FL). Found "
     "the expensive way once (issue #92); static is the only place it stays "
     "learned. Evidence: /tmp/v3-evidence/wp_inflight."},
};

inline constexpr int kLedgerCount = sizeof(kLedger) / sizeof(kLedger[0]);

// The named-hazard predicate: is this id explicitly in the ledger above?
// O(N) over a tiny constexpr table; called at most once per id per pass.
inline constexpr bool IsUnsafe(int id) {
  for (int i = 0; i < kLedgerCount; ++i) {
    if (kLedger[i].id == id) return true;
  }
  return false;
}

// Is this a documented id (fp_plugclass.h FHD_* 0-87)? A blind sweep skips
// the entire documented range: sweeping it gains no information and risks the
// side effects the ledger only partially enumerates. This is the invariant
// that makes "the sweep is read-only and safe over any range" true by
// construction.
inline constexpr bool IsDocumented(int id) { return id >= 0 && id <= kDocumentedMaxId; }

// Human-readable reason, or nullptr if the id is not in the ledger. Used to
// annotate skipped-id lines in wp_* results so the artifact explains itself.
inline constexpr const UnsafeId *Lookup(int id) {
  for (int i = 0; i < kLedgerCount; ++i) {
    if (kLedger[i].id == id) return &kLedger[i];
  }
  return nullptr;
}

inline constexpr const char *HazardName(Hazard h) {
  switch (h) {
    case Hazard::kNullDeref: return "null_deref";
    case Hazard::kModalUI: return "modal_ui";
    case Hazard::kPtrDeref: return "ptr_deref";
    case Hazard::kExternal: return "external";
    case Hazard::kMutates: return "mutates";
    case Hazard::kCrashesHost: return "crashes_host";
    case Hazard::kHangsHost: return "hangs_host";
  }
  return "unknown";
}

// When L2's generated profile is present, its unsafe-id list and this curated
// ledger must agree: every generated fatal must be quarantined here (with its
// hazard prose), or the two sources have drifted and the build - not FL -
// fails. The reverse direction is not asserted: the ledger legitimately names
// hazards (modal, ptr-deref) the profile's live-fatal list does not carry.
#ifdef FLMCP_PROFILE_GENERATED
inline constexpr bool LedgerCoversGeneratedUnsafeIds() {
  for (int i = 0; i < fl_profile::kUnsafeIdCount; ++i) {
    if (!IsUnsafe(fl_profile::kUnsafeIds[i])) return false;
  }
  return true;
}
static_assert(LedgerCoversGeneratedUnsafeIds(),
              "fl_profile_generated.h names an unsafe id the static ledger does not "
              "quarantine - add it to fl_dispatch::kLedger with its hazard");
#endif

}  // namespace fl_dispatch
