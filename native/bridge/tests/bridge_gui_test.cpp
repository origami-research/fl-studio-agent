// Test for the GUI-thread marshal (bridge_gui.h) - the property the
// FHD_AddNotesToPR deadlock fix rests on: work handed over from a non-GUI
// thread runs on the MAIN thread, and not before the main thread's run loop
// gets a turn. FL's run loop is a CFRunLoop, so draining one here exercises
// the same mechanism the plugin depends on live.
//
// Hand-rolled asserts, no test framework - dependency-free convention.

#include <atomic>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

#include "bridge_gui.h"

#if defined(__APPLE__)
#include <CoreFoundation/CoreFoundation.h>
#include <pthread.h>
#endif

namespace {

int g_failures = 0;

#define CHECK(cond)                                                   \
  do {                                                                \
    if (!(cond)) {                                                    \
      fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      g_failures++;                                                   \
    }                                                                 \
  } while (0)

// One pass of the main run loop, which is what drains libdispatch's main queue.
// Bounded, so a marshal that never arrives fails the test instead of hanging it.
void DrainMainQueue(double seconds) {
#if defined(__APPLE__)
  CFRunLoopRunInMode(kCFRunLoopDefaultMode, seconds, false);
#else
  (void)seconds;
#endif
}

}  // namespace

int main() {
  // --- 1. on the main thread, RunOnGuiThread is a plain call -----------------
  // No queueing, no run loop needed: this is the path FL's own (G) callbacks
  // (Idle_Public) take, and it must stay synchronous so ordering is obvious.
  {
    CHECK(bridge_gui::OnGuiThread());
    bool ran = false;
    bridge_gui::RunOnGuiThread([&ran] { ran = true; });
    CHECK(ran);
  }

  // --- 2. from a worker thread, the work is DEFERRED, not run inline ---------
  // The deadlock was a mixer-thread callback reaching into FL. The whole fix is
  // that such a thread hands the work over and returns immediately.
  {
    std::atomic<bool> ran{false};
    std::atomic<bool> saw_gui_thread{false};
    std::atomic<bool> handover_returned{false};
    std::thread worker([&] {
      CHECK(!bridge_gui::OnGuiThread());
      bridge_gui::RunOnGuiThread([&] {
        saw_gui_thread.store(bridge_gui::OnGuiThread());
        ran.store(true);
      });
      handover_returned.store(true);
    });
    worker.join();
    CHECK(handover_returned.load());
#if defined(__APPLE__)
    CHECK(!ran.load());  // nothing runs until the run loop gets a turn
#endif
    DrainMainQueue(1.0);
    CHECK(ran.load());
    CHECK(saw_gui_thread.load());
  }

  // --- 3. queued work keeps its state alive across the handover --------------
  // The Bridge queues a shared_ptr copy so a pump outlives the plugin instance
  // that asked for it; this is that ownership contract in miniature.
  {
    auto owned = std::make_shared<std::string>("survives");
    std::weak_ptr<std::string> watch = owned;
    std::string seen;
    std::thread worker([&] {
      bridge_gui::RunOnGuiThread([owned, &seen] { seen = *owned; });
    });
    worker.join();
    owned.reset();  // the caller lets go before the work has run
#if defined(__APPLE__)
    CHECK(!watch.expired());
#endif
    DrainMainQueue(1.0);
    CHECK(seen == "survives");
    CHECK(watch.expired());  // and released once it has
  }

  // --- 4. many handovers all arrive, in order --------------------------------
  // A burst of ticks must not drop or reorder pumps.
  {
    std::vector<int> order;
    std::thread worker([&] {
      for (int i = 0; i < 32; ++i) bridge_gui::RunOnGuiThread([&order, i] { order.push_back(i); });
    });
    worker.join();
    DrainMainQueue(1.0);
    CHECK(order.size() == 32);
    bool sequential = true;
    for (size_t i = 0; i < order.size(); ++i) {
      if (order[i] != static_cast<int>(i)) sequential = false;
    }
    CHECK(sequential);
  }

  if (g_failures == 0) {
    printf("bridge_gui_test: all checks passed\n");
    return 0;
  }
  fprintf(stderr, "bridge_gui_test: %d failure(s)\n", g_failures);
  return 1;
}
