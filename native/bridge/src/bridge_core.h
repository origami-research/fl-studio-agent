// Bridge core — the host-independent half of the FL MCP Bridge plugin.
//
// Everything here is pure logic over a spool directory and a TFruityPlugHost
// pointer: reading the v2 sequenced spool (commands/queue.json + one file per
// command), the shared consumed-id ledger, converting the server's musical
// wire form (beats + 0..1 levels) into the SDK's TNotesParams (PPQ ticks + FL
// level units), issuing FHD_AddNotesToPR, and writing the per-command
// response. The plugin ABI shell (bridge.cpp) owns none of this so it can be
// exercised under ASan/UBSan against a mock host with a temp spool dir, with
// no FL Studio in the loop (bridge_core_test.cpp, bridge_udp_test.cpp).
//
// The spool protocol is docs/03 / mcp-server/src/fl_mcp/spool.py (proto 2),
// shared with the piano roll .pyscript so either consumer applies a command
// exactly once via the shared ledger. The Bridge serves the same commands
// over TWO carriers — this file spool and the localhost UDP transport
// (bridge_udp.*, docs/22) — through ONE executor (ExecuteCommand) and that
// one ledger; only delivery and disposal differ per carrier.
#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

// The SDK types (TFruityPlugHost, TNotesParams, ...) are anonymous-struct
// typedefs, so they cannot be forward-declared; include the header directly.
#include "fp_plugclass.h"
#include "minijson.h"

namespace bridge_core {

// Must match mcp-server/src/fl_mcp/schemas.py SCHEMA_VERSION and the pyscript's
// PROTO_VER; a mismatched command is claimed and refused with an error response.
constexpr int kProtoVersion = 2;
constexpr int kDefaultPPQ = 96;       // last-resort only; v2 commands carry a ppq stamp
constexpr int kMaxConsumedIds = 64;   // ledger ring size
constexpr int kMaxNotesPerCommand = 5000;  // schemas.AddNotesArgs max_length

// The spool roots the Bridge reads/writes, most-preferred first. Mirrors
// mcp-server/src/fl_mcp/flpaths.py SPOOL_SUBDIRS: FL grants writes under
// "Piano roll scripts" recursively but not elsewhere in Settings.
// `home` is injectable so tests can point at a temp dir instead of the real
// Image-Line tree.
std::vector<std::string> SpoolRoots(const char *home);

// Owns a malloc'd TNotesParams sized for `count` notes. Frees on destruction.
// Exposed (rather than kept internal to the apply path) so the test can inspect
// the exact bytes the Bridge would hand FHD_AddNotesToPR.
struct NotesParamsBuffer {
  TNotesParams *params = nullptr;
  std::size_t bytes = 0;
  int count = 0;
  int ppq = 0;                    // the PPQ the beat->tick conversion used
  bool used_default_ppq = false;  // true when the command carried no ppq stamp

  NotesParamsBuffer() = default;
  NotesParamsBuffer(NotesParamsBuffer &&other) noexcept;
  NotesParamsBuffer &operator=(NotesParamsBuffer &&other) noexcept;
  NotesParamsBuffer(const NotesParamsBuffer &) = delete;
  NotesParamsBuffer &operator=(const NotesParamsBuffer &) = delete;
  ~NotesParamsBuffer();
};

// Build a TNotesParams from a v2 command's `args` object
// ({target:{pattern,channel}, notes:[...], mode, ppq}). `ppq_default` is used
// only when args carries no "ppq" stamp (flagged in the buffer). Conversion:
//   start/length (beats) -> Position/Length (PPQ ticks)
//   velocity 0..1        -> Vol (0..128, FL default 100/128)
//   pan 0..1 (0.5 centre)-> Pan (centred at 0)
//   key                  -> Note; color -> Color; Target=1 (piano roll)
// `empty_first` forces NPF_EmptyFirst (the clear_notes mapping); otherwise
// `mode:"replace"` sets it. target.pattern/target.channel default to -1
// (current pattern / selected channel - the latter is why the Bridge is an
// effect, per fp_plugclass.h TNotesParams.ChanNum).
NotesParamsBuffer BuildNotesParams(const minijson::Value &args, int ppq_default, bool empty_first);

// Outcome of one executed command, for the caller to log / report.
struct PollOutcome {
  bool had_command = false;   // a fresh, not-yet-consumed command was found and handled
  int id = 0;
  bool ok = false;
  int notes_written = 0;
  std::string op;
  std::string error;
};

// --- transport seam (docs/22) ------------------------------------------------
// A carrier's job is delivery and disposal (UDP: ack datagram; file spool:
// the per-id tombstone RecordConsumed writes); ExecuteCommand owns semantics.

// One command as delivered by a transport: the command JSON verbatim, plus an
// opaque reply address the transport uses to ack (UDP: raw sockaddr bytes).
struct InboundDatagram {
  std::string payload;
  std::string reply_to;
};

// Mutex-guarded bounded FIFO between the socket thread and the tick. The socket
// thread only Pushes; the tick only Drains. On overflow the OLDEST entry is
// dropped and counted: newest commands win, a dropped command simply never
// acks (the client retransmits or falls back to the file spool), and the
// ledger keeps a retry from double-applying.
class CommandQueue {
 public:
  explicit CommandQueue(std::size_t capacity) : capacity_(capacity) {}
  void Push(InboundDatagram datagram);
  std::vector<InboundDatagram> Drain(std::size_t max_items);
  std::uint64_t dropped() const;

 private:
  mutable std::mutex mu_;
  std::deque<InboundDatagram> items_;
  std::size_t capacity_;
  std::uint64_t dropped_ = 0;
};

// Ring of the last N response payloads keyed by command id, so a retransmitted
// datagram whose first ack was lost is re-acked verbatim without re-execution.
// Tick-thread only; no locking.
class ResultCache {
 public:
  explicit ResultCache(std::size_t capacity = kMaxConsumedIds) : capacity_(capacity) {}
  const std::string *Find(int id) const;
  void Put(int id, std::string ack_json);

 private:
  std::size_t capacity_;
  std::deque<std::pair<int, std::string>> entries_;
};

// Forensic breadcrumb around every host Dispatcher call. A host call that
// never returns (the FHD_AddNotesToPR deadlock - see bridge_gui.h) leaves no
// stack, no crash report and no response file, so the only evidence a live
// retest can collect is what we wrote just BEFORE entering FL. `phase` is
// "<op>:enter" / "<op>:return"; a checkpoint stuck on ":enter" names the exact
// host call that hung and the id it was carrying.
//
// Set once by the plugin shell at construction; null (silent) in tests that do
// not install one.
using HostCallTrace = void (*)(const char *phase, int id, int notes);
void SetHostCallTrace(HostCallTrace trace);

struct ExecResult {
  PollOutcome outcome;
  // The transport ack payload (the same v2 SpoolResponse-shaped JSON written
  // to responses/<id>.json). Always non-empty: even malformed input gets an
  // explicit error ack.
  std::string ack_json;
  bool duplicate = false;  // id already in the ledger; nothing was executed
};

// THE executor - both carriers feed this with the v2 command JSON verbatim.
// GUI THREAD ONLY: it calls back into the host, which FL forbids from a locked
// mixer callback and which deadlocked live from the extended tick. The shell
// guarantees the thread by construction (bridge_gui.h); nothing here checks.
// Answers an ID-LESS "ping" statelessly, touching neither the ledger nor the
// filesystem nor FL (heartbeat probes must not evict real ids from the ledger
// ring; an id-carrying ping is claimed like any command, which is what prunes
// it from the queue when the server dies first). For id-carrying commands:
// claims the id in the ledger BEFORE anything else - same ordering as the
// pyscript; this is the crash-safety invariant, do not reorder - then checks
// proto, dispatches the op (add_notes/clear_notes via
// host->Dispatcher(host_tag, FHD_AddNotesToPR, ...) + FHD_SetDirty), and
// writes responses/<id>.json in the SpoolResponse shape regardless of
// carrier. Duplicates re-ack from `cache` (nullable) or with a minimal
// consumed ack, and never re-execute.
ExecResult ExecuteCommand(TFruityPlugHost *host, std::intptr_t host_tag,
                          const std::vector<std::string> &roots, int ppq_default,
                          const std::string &payload_json, ResultCache *cache);

// The file-spool carrier: consume the lowest-id fresh command listed in any
// root's commands/queue.json (not in the ledger, file readable and not a
// tombstone - a tombstoned or unreadable id is skipped, never blocking the
// ids behind it) and feed it to ExecuteCommand. Applies at most one command
// per call; the next tick picks up the next. The queue index itself is never
// rewritten here - the server owns it (spool.py drops settled ids). Host may
// be null only in tests that pass a mock; a real null host yields ok=false.
PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default,
                     ResultCache *cache = nullptr);

// --- spool primitives, exposed for tests -------------------------------------
std::vector<int> QueueIds(const std::vector<std::string> &roots);  // sorted, deduped
std::vector<int> ReadConsumedIds(const std::vector<std::string> &roots);
bool AlreadyConsumed(const std::vector<std::string> &roots, int id);
int RecordConsumed(const std::vector<std::string> &roots, int id);  // returns #roots that accepted

}  // namespace bridge_core
