#include "bridge_events.h"

#include <sys/stat.h>

#include <cstdio>

#include "minijson.h"

namespace bridge_events {

bool IsSubscribed(int id) {
  for (int i = 0; i < kSubscribedCount; ++i) {
    if (kSubscribedIds[i] == id) return true;
  }
  return false;
}

const char *EventName(int id) {
  switch (id) {
    case 12: return "FPD_SetPlaying";
    case 13: return "FPD_SongPosChanged";
    case 14: return "FPD_SetTimeSig";
    case 22: return "FPD_SetFocus";
    case 25: return "FPD_RoutingChanged";
    case 27: return "FPD_ProjLoaded";
    case 41: return "FPD_ContextInfoChanged";
    default: return "";
  }
}

// --- EventRing ---------------------------------------------------------------

EventRing::EventRing(unsigned capacity)
    : capacity_(capacity == 0 ? 1 : capacity), ring_(capacity_), seq_(capacity_) {}

void EventRing::Push(int id, int index, int value, int gui, long long ts_ns, long long unix_ms) {
  if (!IsSubscribed(id)) return;
  std::uint32_t claim;
  while (true) {
    // The consumer publishes a completed read before a producer may reuse its
    // slot. Loading it first also prevents a stale claim from being compared
    // with a newer consumer cursor.
    const std::uint32_t drained = drained_.load(std::memory_order_acquire);
    claim = claimed_.load(std::memory_order_relaxed);
    if (claim - drained >= capacity_) {
      dropped_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    if (claimed_.compare_exchange_weak(claim, claim + 1,
                                       std::memory_order_relaxed,
                                       std::memory_order_relaxed)) {
      break;
    }
  }
  std::uint32_t slot = claim % capacity_;
  ring_[slot].ts_ns = ts_ns;
  ring_[slot].unix_ms = unix_ms;
  ring_[slot].id = id;
  ring_[slot].index = index;
  ring_[slot].value = value;
  ring_[slot].gui = gui;
  // Publish: the consumer reads this slot only once its sequence == claim+1.
  seq_[slot].store(claim + 1, std::memory_order_release);
}

std::vector<BridgeEvent> EventRing::Drain(std::size_t max_items) {
  std::vector<BridgeEvent> out;
  while (out.size() < max_items) {
    const std::uint32_t drained = drained_.load(std::memory_order_relaxed);
    std::uint32_t slot = drained % capacity_;
    if (seq_[slot].load(std::memory_order_acquire) != drained + 1) break;
    const Slot &s = ring_[slot];
    BridgeEvent e;
    // The user-facing seq is assigned HERE, on the single consumer, so it is
    // gap-free: a dropped push never burns a seq. Drain order is enqueue order
    // (FIFO ring), so `emitted_` is also the enqueue order. A seq that
    // decreases across reads therefore means a new plugin load (a fresh ring),
    // never a drop - drops surface only in `dropped`. (L3's resync contract.)
    e.seq = ++emitted_;
    e.ts_ns = s.ts_ns;
    e.unix_ms = s.unix_ms;
    e.id = s.id;
    e.index = s.index;
    e.value = s.value;
    e.gui = s.gui;
    e.dropped = dropped_.load(std::memory_order_relaxed);
    out.push_back(e);
    drained_.store(drained + 1, std::memory_order_release);
  }
  emitted_seq_.store(emitted_, std::memory_order_relaxed);
  return out;
}

// --- FormatEventLine ---------------------------------------------------------

std::string FormatEventLine(const BridgeEvent &event) {
  std::string line = "{\"seq\": ";
  line += std::to_string(event.seq);
  line += ", \"ts_ns\": " + std::to_string(event.ts_ns);
  line += ", \"unix_ms\": " + std::to_string(event.unix_ms);
  line += ", \"id\": " + std::to_string(event.id);
  line += ", \"name\": \"" + minijson::Escape(EventName(event.id)) + "\"";
  line += ", \"index\": " + std::to_string(event.index);
  line += ", \"value\": " + std::to_string(event.value);
  line += ", \"gui\": " + std::to_string(event.gui);
  line += ", \"dropped\": " + std::to_string(event.dropped);
  line += "}\n";
  return line;
}

// --- EventLog ----------------------------------------------------------------

namespace {

long FileSize(const std::string &path) {
  struct stat st;
  if (stat(path.c_str(), &st) != 0) return -1;
  return static_cast<long>(st.st_size);
}

}  // namespace

EventLog::EventLog(std::vector<std::string> paths, long cap_bytes) : paths_(std::move(paths)) {
  // Truncate any file already over the cap so a permanently-inserted plugin
  // cannot grow the log without bound across many sessions. Opening "wb" and
  // closing empties the file; smaller files are left untouched (appended to).
  for (const std::string &path : paths_) {
    if (FileSize(path) > cap_bytes) {
      FILE *f = fopen(path.c_str(), "wb");
      if (f != nullptr) fclose(f);
    }
  }
}

void EventLog::AppendBatch(const std::vector<BridgeEvent> &events) {
  if (events.empty()) return;
  std::string batch;
  for (const BridgeEvent &e : events) batch += FormatEventLine(e);
  for (const std::string &path : paths_) {
    FILE *f = fopen(path.c_str(), "ab");
    if (f == nullptr) continue;
    fwrite(batch.data(), 1, batch.size(), f);
    fclose(f);
  }
}

}  // namespace bridge_events
