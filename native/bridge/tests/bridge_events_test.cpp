// Unit test for the event-forwarding ring, its JSONL schema, the log's
// truncation cap, and the heartbeat format. Built with ASan+UBSan so the
// producer/consumer race harness (subscribed FPD_* pushed from N threads while
// one thread drains) is exercised for out-of-bounds and use-after-free. No FL
// in the loop. Hand-rolled asserts, dependency-free convention.

#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "bridge_core.h"
#include "bridge_events.h"

namespace {

int g_failures = 0;

#define CHECK(cond)                                                   \
  do {                                                                \
    if (!(cond)) {                                                    \
      fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      g_failures++;                                                   \
    }                                                                 \
  } while (0)

std::string ReadFile(const std::string &path) {
  std::ifstream f(path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

bool Contains(const std::string &h, const std::string &n) { return h.find(n) != std::string::npos; }

long FileSize(const std::string &path) {
  struct stat st;
  if (stat(path.c_str(), &st) != 0) return -1;
  return static_cast<long>(st.st_size);
}

int CountLines(const std::string &s) {
  int n = 0;
  for (char c : s) {
    if (c == '\n') n++;
  }
  return n;
}

}  // namespace

int main() {
  using namespace bridge_events;

  // --- 1. subscription set + names --------------------------------------------
  {
    CHECK(IsSubscribed(12));  // FPD_SetPlaying
    CHECK(IsSubscribed(13));
    CHECK(IsSubscribed(14));
    CHECK(IsSubscribed(22));
    CHECK(IsSubscribed(25));
    CHECK(IsSubscribed(27));
    CHECK(IsSubscribed(41));
    CHECK(!IsSubscribed(0));
    CHECK(!IsSubscribed(11));   // FPD_VSTiIdle - not forwarded
    CHECK(!IsSubscribed(42));   // FPD_ProjectInfoChanged - not in the set
    CHECK(std::string(EventName(12)) == "FPD_SetPlaying");
    CHECK(std::string(EventName(41)) == "FPD_ContextInfoChanged");
    CHECK(std::string(EventName(999)).empty());
  }

  // --- 2. ring filters, orders, and stamps ------------------------------------
  {
    EventRing ring(16);
    ring.Push(12, 0, 1, 1, 100, 5);   // subscribed
    ring.Push(7, 0, 0, 1, 101, 6);    // NOT subscribed - ignored
    ring.Push(41, 3, 42, 0, 102, 7);  // subscribed, mixing thread (gui=0)
    std::vector<BridgeEvent> out = ring.Drain(64);
    CHECK(out.size() == 2);
    if (out.size() == 2) {
      CHECK(out[0].seq == 1);         // gap-free, starts at 1
      CHECK(out[0].id == 12);
      CHECK(out[0].value == 1);
      CHECK(out[0].gui == 1);
      CHECK(out[0].ts_ns == 100);
      CHECK(out[0].unix_ms == 5);
      CHECK(out[1].seq == 2);         // the unsubscribed push did NOT burn a seq
      CHECK(out[1].id == 41);
      CHECK(out[1].index == 3);
      CHECK(out[1].value == 42);
      CHECK(out[1].gui == 0);
    }
    CHECK(ring.last_seq() == 2);
    CHECK(ring.dropped() == 0);
    // A second drain yields nothing and does not advance seq.
    CHECK(ring.Drain(64).empty());
  }

  // --- 3. overflow drops the newest and counts it, seq stays gap-free ---------
  {
    EventRing ring(4);
    for (int i = 0; i < 10; ++i) ring.Push(12, i, i, 1, i, i);  // 10 into a 4-slot ring
    std::vector<BridgeEvent> out = ring.Drain(64);
    CHECK(out.size() == 4);            // only capacity survives
    CHECK(ring.dropped() == 6);        // the other 6 counted, not torn
    // Surviving seqs are 1..4 with no gap, even though 6 pushes were dropped.
    for (std::size_t i = 0; i < out.size(); ++i) CHECK(out[i].seq == static_cast<std::uint32_t>(i + 1));
    // The last-drained event carries the cumulative drop count for L3.
    if (!out.empty()) CHECK(out.back().dropped == 6);

    // Overflow must not consume a claim: after the full ring drains, later
    // events publish and drain normally instead of sitting behind a hole.
    ring.Push(12, 10, 10, 1, 10, 10);
    ring.Push(12, 11, 11, 1, 11, 11);
    out = ring.Drain(64);
    CHECK(out.size() == 2);
    if (out.size() == 2) {
      CHECK(out[0].seq == 5);
      CHECK(out[0].index == 10);
      CHECK(out[1].seq == 6);
      CHECK(out[1].index == 11);
      CHECK(out[1].dropped == 6);
    }
    CHECK(ring.dropped() == 6);
  }

  // --- 4. JSONL line is the pinned schema -------------------------------------
  {
    BridgeEvent e;
    e.seq = 7;
    e.ts_ns = 123456789;
    e.unix_ms = 1788000000000LL;
    e.id = 41;
    e.index = 3;
    e.value = -1;
    e.gui = 1;
    e.dropped = 2;
    std::string line = FormatEventLine(e);
    CHECK(line.back() == '\n');  // one object per line
    CHECK(Contains(line, "\"seq\": 7"));
    CHECK(Contains(line, "\"ts_ns\": 123456789"));
    CHECK(Contains(line, "\"unix_ms\": 1788000000000"));
    CHECK(Contains(line, "\"id\": 41"));
    CHECK(Contains(line, "\"name\": \"FPD_ContextInfoChanged\""));
    CHECK(Contains(line, "\"index\": 3"));
    CHECK(Contains(line, "\"value\": -1"));
    CHECK(Contains(line, "\"gui\": 1"));
    CHECK(Contains(line, "\"dropped\": 2"));
  }

  // --- 5. EventLog appends, and truncates only an oversized file --------------
  {
    char tmpl[] = "/tmp/bridge_events_test_XXXXXX";
    char *dir = mkdtemp(tmpl);
    CHECK(dir != nullptr);
    std::string path = std::string(dir) + "/bridge_events.jsonl";

    std::vector<BridgeEvent> batch;
    for (int i = 0; i < 3; ++i) {
      BridgeEvent e;
      e.seq = i + 1;
      e.id = 12;
      batch.push_back(e);
    }
    {
      EventLog log({path});
      log.AppendBatch(batch);
      log.AppendBatch(batch);  // a second batch appends, never rewrites
    }
    CHECK(CountLines(ReadFile(path)) == 6);

    // A file already over the cap is truncated at construction; a small one is
    // left intact. Use a tiny cap so the existing 6 lines exceed it.
    {
      EventLog log({path}, /*cap_bytes=*/8);
      CHECK(FileSize(path) == 0);  // truncated because it was over the cap
      log.AppendBatch(batch);
      CHECK(CountLines(ReadFile(path)) == 3);
    }
    // A fresh small file under the cap survives across a new log.
    {
      EventLog log({path}, EventLog::kDefaultCapBytes);
      CHECK(CountLines(ReadFile(path)) == 3);  // not truncated
    }
  }

  // --- 6. producer/consumer race: nothing torn, accounting balances ----------
  // N producer threads push subscribed events while the main thread drains. The
  // invariant that must hold under ASan: every push is either drained exactly
  // once or counted as dropped, seqs are unique and strictly increasing in
  // drain order, and no read lands out of bounds.
  {
    constexpr int kProducers = 4;
    constexpr int kPerProducer = 5000;
    EventRing ring(1024);
    std::atomic<bool> go{false};
    std::atomic<int> remaining{kProducers};
    std::vector<std::thread> producers;
    for (int p = 0; p < kProducers; ++p) {
      producers.emplace_back([&ring, &go, &remaining, p] {
        while (!go.load()) {
        }
        for (int i = 0; i < kPerProducer; ++i) {
          // Alternate a subscribed and an unsubscribed id; only the subscribed
          // ones may ever appear, which also tests the filter under contention.
          ring.Push(12, p, i, 1, i, i);
          ring.Push(99, p, i, 1, i, i);  // not subscribed -> must never enqueue
        }
        remaining.fetch_sub(1, std::memory_order_release);
      });
    }
    go.store(true);
    std::vector<BridgeEvent> drained;
    std::uint32_t last = 0;
    bool monotonic = true;
    // Drain while producers run. This exercises the atomic consumer cursor:
    // every producer observes either available space or a full ring, never a
    // non-atomic concurrent read.
    while (remaining.load(std::memory_order_acquire) != 0) {
      std::vector<BridgeEvent> batch = ring.Drain(256);
      for (const BridgeEvent &e : batch) {
        if (e.seq <= last) monotonic = false;  // strictly increasing, unique
        last = e.seq;
        CHECK(e.id == 12);  // the unsubscribed id never made it in
        drained.push_back(e);
      }
    }
    for (std::thread &t : producers) t.join();
    while (true) {
      std::vector<BridgeEvent> batch = ring.Drain(256);
      if (batch.empty()) break;
      for (const BridgeEvent &e : batch) {
        if (e.seq <= last) monotonic = false;
        last = e.seq;
        CHECK(e.id == 12);
        drained.push_back(e);
      }
    }
    CHECK(monotonic);
    const std::uint64_t pushed = static_cast<std::uint64_t>(kProducers) * kPerProducer;
    CHECK(drained.size() + ring.dropped() == pushed);
  }

  // --- 7. heartbeat carries proto=2 and the pat/chan lines L1 parses ----------
  {
    bridge_core::HeartbeatState hb;
    hb.tick = 42;
    hb.pid = 1234;
    hb.proto = bridge_core::kProtoVersion;
    hb.pattern = 3;    // learned from a bridge write's SetCurrentPattern
    hb.channel = -1;   // no getter exists -> always unknown
    hb.event_seq = 9;
    hb.abi = "ok";
    hb.render_blocks = 12;
    hb.render_rate = 3;
    std::string text = bridge_core::FormatHeartbeat(hb);
    CHECK(Contains(text, "proto=2\n"));
    CHECK(Contains(text, "pat=3\n"));
    CHECK(Contains(text, "chan=-1\n"));
    CHECK(Contains(text, "backend=bridge\n"));
    CHECK(Contains(text, "tick=42\n"));
    CHECK(Contains(text, "event_seq=9\n"));
    CHECK(Contains(text, "abi=ok\n"));
    CHECK(Contains(text, "render_blocks=12\n"));
    CHECK(Contains(text, "render_rate=3\n"));
    // Unknown pattern is -1, the same sentinel L1 maps to None.
    bridge_core::HeartbeatState fresh;
    CHECK(Contains(bridge_core::FormatHeartbeat(fresh), "pat=-1\n"));
  }

  if (g_failures == 0) {
    printf("bridge_events_test: all checks passed\n");
    return 0;
  }
  fprintf(stderr, "bridge_events_test: %d failure(s)\n", g_failures);
  return 1;
}
