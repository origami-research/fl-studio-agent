// Bridge core — the host-independent half of the FL MCP Bridge plugin.
//
// Everything here is pure logic over a spool directory and a TFruityPlugHost
// pointer: locating/parsing the pending command, the shared consumed-id ledger,
// converting the server's musical wire form (beats + 0..1 levels) into the
// SDK's TNotesParams (PPQ ticks + FL level units), issuing FHD_AddNotesToPR,
// and writing the result. The plugin ABI shell (bridge.cpp) owns none of this
// so it can be exercised under ASan/UBSan against a mock host with a temp spool
// dir, with no FL Studio in the loop (bridge_core_test.cpp).
//
// The spool protocol is docs/03 / mcp-server/src/fl_mcp/spool.py, shared
// byte-for-byte with the piano roll .pyscript so either consumer applies a
// command exactly once via the shared ledger.
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

constexpr int kProtoVersion = 1;
constexpr int kDefaultPPQ = 96;       // FL's default piano-roll PPQ; overridable per command
constexpr int kMaxConsumedIds = 64;   // ledger ring size
constexpr int kMaxNotesPerCommand = 4096;

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

  NotesParamsBuffer() = default;
  NotesParamsBuffer(NotesParamsBuffer &&other) noexcept;
  NotesParamsBuffer &operator=(NotesParamsBuffer &&other) noexcept;
  NotesParamsBuffer(const NotesParamsBuffer &) = delete;
  NotesParamsBuffer &operator=(const NotesParamsBuffer &) = delete;
  ~NotesParamsBuffer();
};

// Build a TNotesParams from an "add_notes"/"clear_notes"-shaped command object.
// `ppq_default` is used when the command carries no "ppq" field. Conversion:
//   start/length (beats) -> Position/Length (PPQ ticks)
//   velocity 0..1        -> Vol (0..128, FL default 100/128)
//   pan 0..1 (0.5 centre)-> Pan (centred at 0)
//   key                  -> Note; color -> Color; Target=1 (piano roll)
// `mode:"replace"` sets NPF_EmptyFirst; "pattern"/"channel" default to -1
// (current pattern / selected channel - the latter is why the Bridge is an
// effect, per fp_plugclass.h TNotesParams.ChanNum).
NotesParamsBuffer BuildNotesParams(const minijson::Value &command, int ppq_default);

// Outcome of one PollOnce, for the caller to log / report.
struct PollOutcome {
  bool had_command = false;   // a fresh, not-yet-consumed command was found and handled
  int id = 0;
  bool ok = false;
  int notes_written = 0;
  std::string op;
  std::string error;
};

// Read the newest pending command across `roots`; if it is fresh (proto matches,
// id not in the ledger), claim it in the ledger (before executing, so a command
// that crashes FL is never replayed), apply it via
// host->Dispatcher(host_tag, FHD_AddNotesToPR, ...), FHD_SetDirty, and write
// result.json. Applies at most one command per call. Host may be null only in
// tests that pass a mock; a real null host yields ok=false.
PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default);

// --- ledger primitives, exposed for tests -----------------------------------
std::vector<int> ReadConsumedIds(const std::vector<std::string> &roots);
bool AlreadyConsumed(const std::vector<std::string> &roots, int id);
int RecordConsumed(const std::vector<std::string> &roots, int id);  // returns #roots that accepted

}  // namespace bridge_core
