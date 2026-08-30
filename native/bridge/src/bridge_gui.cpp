#include "bridge_gui.h"

#include <utility>

#if defined(__APPLE__)
#include <dispatch/dispatch.h>
#include <pthread.h>
#endif

namespace bridge_gui {

#if defined(__APPLE__)

bool OnGuiThread() { return pthread_main_np() != 0; }

void RunOnGuiThread(std::function<void()> fn) {
  if (OnGuiThread()) {
    fn();
    return;
  }
  // libdispatch's main queue is drained by the main thread's run loop, which
  // every Cocoa app (FL included) runs continuously. dispatch_async_f rather
  // than a block literal: plain C++, no Blocks runtime, and the heap-owned
  // std::function keeps the callable alive until it has run exactly once.
  auto *owned = new std::function<void()>(std::move(fn));
  dispatch_async_f(dispatch_get_main_queue(), owned, [](void *context) {
    auto *queued = static_cast<std::function<void()> *>(context);
    (*queued)();
    delete queued;
  });
}

#else

// The Bridge only ships for FL on macOS; this branch exists so the shared
// sources still compile elsewhere (CI, a Linux dev box running the unit
// tests). With no run loop to hand work to, "the GUI thread" is the caller's.
bool OnGuiThread() { return true; }

void RunOnGuiThread(std::function<void()> fn) { fn(); }

#endif

}  // namespace bridge_gui
