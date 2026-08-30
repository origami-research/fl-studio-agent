// GUI-thread marshal - "every host call happens on the GUI thread", true by
// construction rather than by luck about which callback FL happens to drive.
//
// WHY THIS FILE'S ORIGINAL POST-MORTEM WAS WRONG (corrected 2026-08-29)
//
// This file was written to fix the "FHD_AddNotesToPR deadlock": live 2026-08, a
// host call issued from the tick NEVER RETURNED, FL stayed up, and the
// heartbeat froze forever. The explanation recorded here was the SDK's
// (M)-callback rule - that the tick was a mixer-side callback, that FL held the
// plugin lock across it, and that calling the host from there was the forbidden
// re-entry.
//
// That explanation is REFUTED, twice over:
//
//  1. The tick is not a mixer callback. What we called "slot 27" is VMT byte
//     216, which on FL's FPC ABI is the SDK's own `Idle_Public` - a documented
//     (G) callback. FLEngine has exactly ONE piece of code that calls it on the
//     registered-plugin list, and the live crash stack shows that code being
//     driven by the Cocoa main run loop (CoreFoundation -> Foundation ->
//     MacVclCocoa -> FLEngine). There is no audio-thread path to it at all.
//     `tick_on_gui=1` is the expected reading, not a surprise.
//
//  2. There was never a lock to deadlock on. `host->Dispatcher(...)` resolved
//     to VMT slot 0, which on an FPC host is `vInstanceSize` - a size, not a
//     function. The call branched into a non-code value and never reached FL.
//     See native/abi/fl_host_abi.h for the contract and its evidence.
//
// So why did it look like a hang instead of a crash? Because FL's Free Pascal
// RTL implements exceptions with setjmp/longjmp (FLEngine's helpers at +0xc820
// and +0xc860 are a textbook aarch64 setjmp/longjmp pair, and both FL's idle
// loop and its idle driver push handlers with the matching
// pushexceptaddr/setjmp/reraise sequence). A longjmp unwinds STRAIGHT PAST our
// C++ frames: `catch (...)` cannot see it, our destructors do not run, and the
// function that made the call never returns and never writes its heartbeat.
// "The host call never returned and the heartbeat froze permanently" is
// therefore exactly what a fault at that branch looks like from our side, on
// any thread, with no deadlock involved. BRIDGE_FINDINGS.md carries the full
// reconciliation of the hang and the later crashes as one cause.
//
// WHY THE MARSHAL STAYS
//
// Not for the reason it was written, but it earns its place anyway:
//
//  * `Eff_Render` (VMT +256) genuinely IS a mixer callback, and it is a real
//    entry point on this object. Anything that ever polls from there must not
//    call the host, and this primitive is what makes that impossible rather
//    than merely intended.
//  * It costs one branch. On FL 2026 the tick already IS the GUI thread, so
//    RunOnGuiThread runs the pump inline with no queueing - the marshal is a
//    passthrough today and a guardrail if a future FL build ticks us from
//    somewhere else.
//  * It is the reason a queued pump can outlive the instance that queued it
//    safely (shared TickState ownership), which the Bridge relies on.
#pragma once

#include <functional>

namespace bridge_gui {

// True when the caller is FL's GUI thread. On macOS that is the process main
// thread: every Cocoa/AppKit app runs its UI there, and FL 2026 is no
// exception.
bool OnGuiThread();

// Run `fn` on the GUI thread: inline when already there, otherwise queued onto
// the main run loop and run at its next pass. Never blocks the caller - a
// mixer-thread tick hands the work over and returns immediately, which is the
// whole point (an audio callback must not wait on the UI).
//
// `fn` must own everything it touches (the Bridge passes a shared_ptr copy) -
// it can outlive the plugin instance that queued it.
void RunOnGuiThread(std::function<void()> fn);

}  // namespace bridge_gui
