// Bridge core — the host-independent half of the FL MCP Bridge plugin.
//
// Everything here is pure logic over a spool directory and a TFruityPlugHost
// pointer: reading the v2 sequenced spool (commands/queue.json + one file per
// command), the shared consumed-id ledger, converting the server's musical
// wire form (beats + 0..1 levels) into the SDK's TNotesParams (PPQ ticks + FL
// level units), issuing FHD_AddNotesToPR, and writing the per-command
// response. The plugin ABI shell (bridge.cpp) owns none of this so it can be
// exercised under ASan/UBSan against a mock host with a temp spool dir, with
// no FL Studio in the loop (bridge_core_test.cpp).
//
// The spool protocol is docs/03 / mcp-server/src/fl_mcp/spool.py (proto 2),
// shared with the piano roll .pyscript so either consumer applies a command
// exactly once via the shared ledger.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
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

// Outcome of one PollOnce, for the caller to log / report.
struct PollOutcome {
  bool had_command = false;   // a fresh, not-yet-consumed command was found and handled
  int id = 0;
  bool ok = false;
  int notes_written = 0;
  std::string op;
  std::string error;
};

// Consume the lowest-id fresh command listed in any root's commands/queue.json:
// if it is fresh (id not in the ledger, file not a tombstone), claim it in the
// ledger (BEFORE executing, so a command that crashes FL is never replayed),
// check proto, apply it via host->Dispatcher(host_tag, FHD_AddNotesToPR, ...),
// FHD_SetDirty, and write responses/<id>.json in the SpoolResponse shape the
// server's FLSession.await_response reads. Applies at most one command per
// call; the next tick picks up the next. The queue index itself is never
// rewritten here - the server owns it (spool.py drops settled ids). Host may
// be null only in tests that pass a mock; a real null host yields ok=false.
PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default);

// --- spool primitives, exposed for tests -------------------------------------
std::vector<int> QueueIds(const std::vector<std::string> &roots);  // sorted, deduped
std::vector<int> ReadConsumedIds(const std::vector<std::string> &roots);
bool AlreadyConsumed(const std::vector<std::string> &roots, int id);
int RecordConsumed(const std::vector<std::string> &roots, int id);  // returns #roots that accepted

}  // namespace bridge_core
