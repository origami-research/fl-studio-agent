// GUI-thread marshal - the one primitive that makes "we never call the host
// from a locked mixer callback" true by construction.
//
// Why this file exists (the FHD_AddNotesToPR deadlock, live 2026-08):
// `host->Dispatcher(HostTag, FHD_AddNotesToPR, ...)` issued from the Bridge's
// extended tick (slot 27) NEVER RETURNED. FL itself stayed alive and
// responsive; only our calling thread was wedged. The SDK header says why, in
// TFruityPlug's own comment block (fp_plugclass.h, above the classic virtuals):
//
//   (G) = called from GUI thread, (M) = called from mixer thread
//   (M) calls are done inside the plugin lock (LockPlugin / UnlockPlugin)
//   + don't call back to the host while inside a LockPlugin / UnlockPlugin block
//
// Slot 27 fires at audio-buffer rate (~120/s live, vs ~0.8/s with the engine
// idle), i.e. it is a mixer-side (M) callback and therefore runs inside FL's
// plugin lock. Calling FHD_AddNotesToPR from there is precisely the
// call-back-into-the-host-while-locked the SDK forbids: the piano roll edit
// needs the GUI/score side, which orders its locks the other way, so the two
// meet and neither moves. Everything the Bridge does that does NOT touch the
// host (spool reads, the ledger, ping) worked fine from that thread - which is
// exactly the observed live signature.
//
// The fix is not a check at the call site; it is where the executor runs. The
// Bridge routes every tick through RunOnGuiThread, so the executor - and with
// it every host Dispatcher call - only ever runs on the GUI thread, outside
// any FL lock. Callbacks FL makes on the GUI thread (Idle_Public, slot 2)
// therefore execute inline with no queueing at all.
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
