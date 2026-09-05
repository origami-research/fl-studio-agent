// Bridge core - the host-independent half of the FL MCP Bridge plugin.
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
// over TWO carriers - this file spool and the localhost UDP transport
// (bridge_udp.*, docs/22) - through ONE executor (ExecuteCommand) and that
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
#include FL_MCP_PLUGIN_API_HEADER

// L2's generated capability profile (gen_layout output): version-specific
// constants + the struct-assert wall. Included here after the SDK API so a
// TU that defines FLMCP_ASSERT_STRUCTS before including this header gets the
// asserts materialized against the SDK types. Absent (pre-integration), the
// fallback constants below stand in and the tree builds in isolation.
#if __has_include("fl_profile_generated.h")
#include "fl_profile_generated.h"
#endif

#include "minijson.h"

namespace bridge_core {

// Must match mcp-server/src/fl_mcp/schemas.py SCHEMA_VERSION and the pyscript's
// PROTO_VER; a mismatched command is claimed and refused with an error response.
constexpr int kProtoVersion = 2;
// Last-resort only; v2 commands carry a ppq stamp. ONE home when the generated
// profile is present (docs/27 s6), warned fallback otherwise.
#ifdef FLMCP_PROFILE_GENERATED
constexpr int kDefaultPPQ = fl_profile::kDefaultPPQ;
#else
constexpr int kDefaultPPQ = 96;
#endif
constexpr int kMaxConsumedIds = 64;   // ledger ring size
constexpr int kMaxNotesPerCommand = 5000;  // schemas.AddNotesArgs max_length

struct CaptureReceipt {
  bool ok = false;
  std::string path;
  std::uint64_t frames_written = 0;
  std::uint64_t frames_requested = 0;
  std::uint64_t drop_count = 0;
  int sample_rate = 0;
  std::string error;
};

// The ABI shell owns audio capture because only it receives Eff_Render. The
// spool executor owns its command protocol, through this narrow seam.
class CaptureControl {
 public:
  virtual ~CaptureControl() = default;
  virtual CaptureReceipt Start(double seconds, int sample_rate_hint, const std::string &root,
                               int command_id) = 0;
  virtual CaptureReceipt Stop() = 0;
};

// The spool roots the Bridge reads/writes, most-preferred first. Mirrors
// mcp-server/src/fl_mcp/flpaths.py SPOOL_SUBDIRS: FL grants writes under
// "Piano roll scripts" recursively but not elsewhere in Settings.
// `home` is injectable so tests can point at a temp dir instead of the real
// Image-Line tree. A non-empty `spool_root` is a per-session override and
// deliberately replaces the fallback roots: a session has one mailbox.
std::vector<std::string> SpoolRoots(const char *home, const char *spool_root = nullptr);

// Owns a malloc'd TNotesParams sized for `count` notes. Frees on destruction.
// Exposed (rather than kept internal to the apply path) so the test can inspect
// the exact bytes the Bridge would hand FHD_AddNotesToPR.
struct NotesParamsBuffer {
  TNotesParams *params = nullptr;
  std::size_t bytes = 0;
  int count = 0;
  int ppq = 0;                    // the PPQ the beat->tick conversion used
  bool used_default_ppq = false;  // true when the command carried no ppq stamp
  const char *error = nullptr;    // why params is null; a literal, never owned

  NotesParamsBuffer() = default;
  NotesParamsBuffer(NotesParamsBuffer &&other) noexcept;
  NotesParamsBuffer &operator=(NotesParamsBuffer &&other) noexcept;
  NotesParamsBuffer(const NotesParamsBuffer &) = delete;
  NotesParamsBuffer &operator=(const NotesParamsBuffer &) = delete;
  ~NotesParamsBuffer();
};

// TNoteParams.Vol is a SINGLE (0..1, FL's default note velocity is 100/128),
// not the `int` the public C++ SDK header declares. That header field is a
// transcription bug: FLEngine_x64.dylib carries its own FPC record RTTI for
// TNoteParams, and it types the field at offset 12 as Single while every other
// field matches the header exactly (BRIDGE_FINDINGS.md has the decode). Same
// four bytes, different type - so the Vol slot is read and written only
// through these two functions, never through the header's declaration.
void SetNoteVol(TNoteParams *note, float vol);
float NoteVol(const TNoteParams &note);

// Build a TNotesParams from a v2 command's `args` object
// ({target:{pattern,channel}, notes:[...], mode, ppq}). `ppq_default` is used
// only when args carries no "ppq" stamp (flagged in the buffer). Conversion:
//   start/length (beats) -> Position/Length (PPQ ticks)
//   velocity 0..1        -> Vol (Single 0..1, FL default 100/128) - same scale,
//                           so this one is a clamp, not a conversion
//   pan 0..1 (0.5 centre)-> Pan (centred at 0)
//   key                  -> Note; color -> Color; Target=1 (piano roll)
// `empty_first` forces NPF_EmptyFirst (the clear_notes mapping); otherwise
// `mode:"replace"` sets it.
//
// THE TARGET MUST BE CONCRETE: `target.pattern` >= 1 and `target.channel` >= 0,
// exactly the bounds schemas.Target already guarantees ("channel names and the
// 'current' default are resolved AT THE TOOL BOUNDARY, never carried as a -1
// sentinel into the core"). Anything else returns a null `params` with `error`
// set, and the command is refused with that message. The SDK does define -1 as
// "current pattern / selected channel", but nothing makes FL able to resolve
// it - there may be no selected channel - and an unresolved -1 used as an array
// index takes the whole DAW down with an access violation, which is strictly
// worse than an error response. So the Bridge never sends one.
NotesParamsBuffer BuildNotesParams(const minijson::Value &args, int ppq_default, bool empty_first);

// Outcome of one executed command, for the caller to log / report.
struct PollOutcome {
  bool had_command = false;   // a fresh, not-yet-consumed command was found and handled
  int id = 0;
  bool ok = false;
  int notes_written = 0;
  // The pattern this command made current, or -1. A successful note write to
  // PatNum >= 1 switches FL's current pattern (the SetCurrentPattern side
  // effect decoded from FL's id-17 handler, FINDINGS); the plugin tracks it so
  // the heartbeat can publish `pat=` for L1's detect() without a getter (no
  // host call reads the current pattern - VERDICT Q2).
  int pattern_after = -1;
  std::string op;
  std::string error;
};

// Everything the heartbeat file publishes, so the format is pure string work a
// test can pin without the plugin shell. The three fields the shared-surfaces
// contract (docs/27 s6) adds for L1's detect() are `proto`, `pattern` and
// `channel`; `pattern`/`channel` are -1 when unknown (channel is ALWAYS -1 -
// FL exposes no selected-channel getter, so the router proves the channel via
// the wire, not the heartbeat).
struct HeartbeatState {
  long long tick = 0;
  int applied = 0;
  int notes = 0;
  int pid = 0;
  long long unix_time = 0;
  int udp_port = 0;
  long long udp_dropped = 0;
  long long gui_pumps = 0;
  long long gui_pending = 0;
  int tick_on_gui = 0;
  int instances = 0;
  int proto = kProtoVersion;
  int pattern = -1;
  int channel = -1;
  long long event_seq = 0;    // highest forwarded event seq (resync point for L3)
  long long event_dropped = 0;
  const char *abi = "probing";
  long long render_blocks = 0;
  long long render_rate = 0;
  int capture = 0;
  long long capture_frames = 0;
};

// The heartbeat text, newline-terminated key=value lines. The key set and
// order are a consumed contract (L1's detect() parses proto/pat/chan; existing
// tools read the rest), so a test pins them.
std::string FormatHeartbeat(const HeartbeatState &state);

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
// fl_abi::HostDispatch(host, host_tag, FHD_AddNotesToPR, ...) + FHD_SetDirty -
// see native/abi/fl_host_abi.h for why it is never `host->Dispatcher(...)`), and
// writes responses/<id>.json in the SpoolResponse shape regardless of
// carrier. Duplicates re-ack from `cache` (nullable) or with a minimal
// consumed ack, and never re-execute.
ExecResult ExecuteCommand(TFruityPlugHost *host, std::intptr_t host_tag,
                          const std::vector<std::string> &roots, int ppq_default,
                          const std::string &payload_json, ResultCache *cache,
                          CaptureControl *capture = nullptr);

// The file-spool carrier: consume the lowest-id fresh command listed in any
// root's commands/queue.json (not in the ledger, file readable and not a
// tombstone - a tombstoned or unreadable id is skipped, never blocking the
// ids behind it) and feed it to ExecuteCommand. Applies at most one command
// per call; the next tick picks up the next. The queue index itself is never
// rewritten here - the server owns it (spool.py drops settled ids). Host may
// be null only in tests that pass a mock; a real null host yields ok=false.
PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default,
                     ResultCache *cache = nullptr, CaptureControl *capture = nullptr);

// --- spool primitives, exposed for tests -------------------------------------
std::vector<int> QueueIds(const std::vector<std::string> &roots);  // sorted, deduped
std::vector<int> ReadConsumedIds(const std::vector<std::string> &roots);
bool AlreadyConsumed(const std::vector<std::string> &roots, int id);
int RecordConsumed(const std::vector<std::string> &roots, int id);  // returns #roots that accepted

}  // namespace bridge_core
