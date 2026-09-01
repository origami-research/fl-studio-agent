// FL push-event forwarding: FL's Dispatcher callbacks -> bridge_events.jsonl.
//
// FL 2026 pushes the plugin, on the GUI thread (and, for a few ids, the mixing
// thread), for exactly the events a co-editing agent needs (VERDICT Q3):
// transport, attention, structure. The bridge forwards the subscribed subset
// to an append-only JSONL file the observer (L3) tails. The schema is the one
// pinned with L3 (docs/27 s5-6):
//
//   {"seq":<u>,"ts_ns":<i>,"unix_ms":<i>,"id":<FPD>,"name":"FPD_...",
//    "index":<i>,"value":<i>,"gui":<0|1>,"dropped":<cumulative>}
//
// one object per line, no coalescing (every subscribed push is one line),
// `seq` monotonic from 1 per plugin load, `dropped` the cumulative ring-drop
// count so a truncated stream says so rather than looking complete.
//
// THREAD DISCIPLINE (the reason this is a lock-free ring, not a mutex): FL
// marks FPD_SetPlaying / FPD_SongPosChanged / FPD_SetTimeSig as (GM) -
// callable from the MIXING thread. The enqueue therefore must never take a
// lock, allocate, or block: a stall on FL's audio thread is a dropout in the
// user's session. So Push only stamps a lock-free ring slot; the GUI-thread
// tick drains the ring to disk. This is exactly the discipline the folded
// probe used for the same callbacks, kept because the constraint is the same.
#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace bridge_events {

// The subscribed FPD ids (docs/27 s5). Raw ids on the wire; L3 owns the
// mapping to UserAction kinds. Kept in sync with EventName() below.
inline constexpr int kSubscribedIds[] = {
    12,  // FPD_SetPlaying        value = playing 0|1 (GM)
    13,  // FPD_SongPosChanged    position relocated  (GM)
    22,  // FPD_SetFocus          value = focused 0|1
    41,  // FPD_ContextInfoChanged index = CI_ id
    25,  // FPD_RoutingChanged    value = RCV_ flags
    27,  // FPD_ProjLoaded
    14,  // FPD_SetTimeSig        value = PTimeSigInfo* (opaque; logged as intptr)
};
inline constexpr int kSubscribedCount = sizeof(kSubscribedIds) / sizeof(kSubscribedIds[0]);

// Is this FPD id one the bridge forwards? Called at the enqueue site, so the
// ring only ever holds subscribed events.
bool IsSubscribed(int id);

// The FPD_* name for a subscribed id, or "" for anything else. The name rides
// the wire so a human reading bridge_events.jsonl needs no id table.
const char *EventName(int id);

// One forwarded push. Value semantics are per-id (see kSubscribedIds); the
// ring stores them uninterpreted.
struct BridgeEvent {
  std::uint32_t seq = 0;   // monotonic per load, starts at 1
  long long ts_ns = 0;     // CLOCK_MONOTONIC nanoseconds at enqueue
  long long unix_ms = 0;   // wall-clock milliseconds at enqueue
  int id = 0;
  int index = 0;
  int value = 0;
  int gui = 0;             // 1 when enqueued on the GUI/main thread
  std::uint64_t dropped = 0;  // cumulative ring drops as of this event's drain
};

// Lock-free MPSC ring between the Dispatcher callback (producers, any thread)
// and the tick (single consumer). Producers reserve a slot with a bounded CAS,
// fill it, then publish with a release store of the claim+1 into the slot's
// sequence; the consumer reads a slot only once its sequence matches. A
// half-written slot is never drained and no producer ever blocks. On overflow
// the NEWEST event (the one that would overwrite an undrained slot) is dropped
// and counted without advancing the producer sequence, so a later event cannot
// be stranded behind an unpublished slot.
class EventRing {
 public:
  explicit EventRing(unsigned capacity = 4096);

  // Enqueue an event if `id` is subscribed; ignore it otherwise. `ts_ns`,
  // `unix_ms` and `gui` are captured by the caller (which knows the thread) so
  // this class stays free of the clock and thread dependencies its test would
  // otherwise need to fake. Audio-thread safe: no lock, no allocation.
  void Push(int id, int index, int value, int gui, long long ts_ns, long long unix_ms);

  // Drain up to `max_items` events in seq order (consumer/GUI thread only).
  // Each returned event carries the cumulative `dropped` count so the sink can
  // stamp it without a second read.
  std::vector<BridgeEvent> Drain(std::size_t max_items);

  std::uint64_t dropped() const { return dropped_.load(std::memory_order_relaxed); }
  // Highest seq emitted so far (the resync point published in the heartbeat).
  std::uint32_t last_seq() const { return emitted_seq_.load(std::memory_order_relaxed); }
  unsigned capacity() const { return capacity_; }

 private:
  struct Slot {
    long long ts_ns = 0;
    long long unix_ms = 0;
    int id = 0;
    int index = 0;
    int value = 0;
    int gui = 0;
  };

  unsigned capacity_;
  std::vector<Slot> ring_;
  std::vector<std::atomic<std::uint32_t>> seq_;  // 0 = empty; claim+1 = published
  std::atomic<std::uint32_t> claimed_{0};        // next claim number (producers)
  std::atomic<std::uint32_t> drained_{0};        // consumer cursor, read by producers
  std::uint32_t emitted_ = 0;                    // consumer-assigned user seq (gap-free)
  std::atomic<std::uint32_t> emitted_seq_{0};    // emitted_ snapshot for last_seq()
  std::atomic<std::uint64_t> dropped_{0};
};

// Format one event as a single JSONL line, trailing newline included. Pure and
// testable: the exact bytes L3 parses.
std::string FormatEventLine(const BridgeEvent &event);

// Append-only sink over the bridge_events.jsonl file in each spool root. On
// construction any file already larger than `cap_bytes` is truncated (a
// bounded-disk guarantee for a plugin that lives on an insert permanently);
// smaller files are left intact and appended to. seq restarts at 1 every load
// regardless, so a seq that decreases within a file marks a new plugin
// session - which is exactly how L3 detects a re-open.
class EventLog {
 public:
  // ~4 MB default cap: large enough to hold a long live window, small enough
  // that an all-day session cannot grow it without bound.
  static constexpr long kDefaultCapBytes = 4L * 1024 * 1024;

  explicit EventLog(std::vector<std::string> paths, long cap_bytes = kDefaultCapBytes);

  // Append the formatted lines for `events` to every path. One fopen/append
  // per path per batch (the tick batches a drain), never per event.
  void AppendBatch(const std::vector<BridgeEvent> &events);

  const std::vector<std::string> &paths() const { return paths_; }

 private:
  std::vector<std::string> paths_;
};

// The bridge_events.jsonl file name, shared by the sink and any reader.
inline constexpr char kEventsFileName[] = "bridge_events.jsonl";

}  // namespace bridge_events
