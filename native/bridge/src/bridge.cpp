// FL MCP Bridge - the production note-writing plugin. Removes the human from
// the loop: it consumes the same spool the piano roll .pyscript reads and
// applies note edits to the live FL session on its own, silently, with no
// editor and no user trigger.
//
// ABI: base-free, correct-by-construction for FL 2026 (26.1.5.x, Apple
// Silicon). This is the outcome of the 12-run Spike 1 crash saga documented in
// native/spike1/NOBASE_FINDINGS.md and BRIDGE_FINDINGS.md next to this file:
//
//   * NO SDK base class. TCPPFruityPlug's construction (or its base
//     TFruityPlug's virtual destructor, which lands two host-callable slots at
//     20/21) crashed every one of the first 11 builds inside FL's own window
//     setup, with zero frames of ours. This class reimplements the TFruityPlug
//     ABI directly: identical data layout (static_asserted against the SDK
//     header) and the 20 classic virtuals in SDK order, with NO virtual
//     destructor anywhere.
//   * 64 vtable slots. FL 2026's real TFruityPlug has grown well past the
//     public header's 20 virtuals - its loader host-calls extended slots
//     (observed live: 26, 27, 30, 32; the RE found call sites for 25/26/31).
//     A 22-slot vtable let those calls run off the end into adjacent RTTI data
//     and branch there (the run-11 EBusError at typeinfo+0). Slots 20..63 are
//     real virtuals here, so every extended call lands on a defined method.
//   * ExtReserved object tail: FL reads a pointer out of the object's +0xa0
//     region on the extended path and may write host-side state past the
//     classic fields; this zeroed tail keeps such access inside memory we own.
//
// THREADING: the tick is VMT byte 216 = the SDK's own `Idle_Public`, a (G)
// callback that FL 2026 drives from the Cocoa main run loop - NOT a mixer-side
// callback, and there was never a plugin lock involved (the earlier
// deadlock-by-lock-re-entry story is refuted; bridge_gui.h carries the
// corrected post-mortem). The tick still does no host work itself: it hands one
// pump at a time to the GUI thread and returns. That keeps "host calls happen
// on the GUI thread" true by construction rather than by luck about which
// callback FL drives - `Eff_Render` (byte 256) genuinely IS (M) and is a real
// entry point on this object.
//
// The note-writing and spool logic lives in bridge_core.* so it is unit-tested
// under ASan/UBSan against a mock host (bridge/tests/bridge_core_test.cpp) with
// no FL in the loop. This file is only the ABI shell + the extended-slot map.

#if defined(_WIN32)
#include <windows.h>
#include <sys/stat.h>
#else
#include <sys/stat.h>
#include <unistd.h>
#endif

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <memory>
#include <string>
#include <vector>

#include "bridge_core.h"
#include "bridge_abi.h"
#include "bridge_capture.h"
#include "fl_host_abi.h"
#include "bridge_events.h"
#include "bridge_gui.h"
#include "bridge_udp.h"
#include "fp_plugclass.h"

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#endif

namespace {

// --- checkpoints -------------------------------------------------------------
// Kept deliberately small and capped: this plugin lives on a mixer insert
// permanently, so unbounded logging is not acceptable. Status is a single
// overwritten value; the heartbeat is a single overwritten counter; the
// extended-slot discovery log is capped per slot. All under the spool dir so a
// human retest can read the plugin's state without a debugger.

char gSpoolDir[512] = {0};
std::vector<std::string> gSpoolRoots;

void BuildSpoolState() {
  const char *home = getenv("HOME");
#if defined(_WIN32)
  if (home == nullptr || home[0] == '\0') home = getenv("USERPROFILE");
#endif
  const char *root = getenv("FLMCP_SPOOL_ROOT");
  gSpoolRoots = bridge_core::SpoolRoots(home, root);
  if (gSpoolRoots.empty()) return;
  snprintf(gSpoolDir, sizeof(gSpoolDir), "%s", gSpoolRoots.front().c_str());
}

void WriteSmall(const char *name, const char *text) {
  if (gSpoolDir[0] == '\0') return;
  char path[600];
  snprintf(path, sizeof(path), "%s/%s", gSpoolDir, name);
  FILE *f = fopen(path, "w");
  if (f == nullptr) return;
  fputs(text, f);
  fclose(f);
}

void WriteStatus(const char *status) {
  char text[96];
  snprintf(text, sizeof(text), "status=%s\n", status);
  WriteSmall("bridge_status.txt", text);
}

// Slot-call log, capped per slot. FL indexes this object as a Free Pascal class
// (VMT byte 200 + 8*i, see native/abi/fl_host_abi.h), so this records WHICH
// offsets a given FL build actually drives - the evidence that decoded the ABI
// in the first place, and the discriminator if a future build shifts. `gui=`
// records the calling thread, which is how an offset is classified (G) or (M).
constexpr int kExtSlotCount = 64;
constexpr int kExtLogCapPerSlot = 4;
int gExtSlotHits[kExtSlotCount] = {0};

void LogExtSlot(int slot, intptr_t a, intptr_t b, intptr_t c) {
  if (slot < 0 || slot >= kExtSlotCount) return;
  if (gExtSlotHits[slot] >= kExtLogCapPerSlot) return;
  gExtSlotHits[slot]++;
  if (gSpoolDir[0] == '\0') return;
  char path[600];
  snprintf(path, sizeof(path), "%s/bridge_ext_slots.txt", gSpoolDir);
  FILE *f = fopen(path, "a");
  if (f == nullptr) return;
  fprintf(f, "ext slot=%d a=%lld b=%lld c=%lld gui=%d\n", slot, (long long)a, (long long)b,
          (long long)c, bridge_gui::OnGuiThread() ? 1 : 0);
  fclose(f);
}

// The extended tick (FL 2026 slot 27) fires often; polling the filesystem on
// every one is wasteful. Poll every Nth tick - still sub-second responsiveness.
constexpr int kPollEveryNTicks = 3;

// Bounded work per pump: a UDP burst must never turn one GUI-thread pump into
// a stall of FL's UI (docs/22 s2). The rest of the burst drains on later pumps.
constexpr std::size_t kMaxUdpCommandsPerPump = 4;
constexpr std::size_t kUdpQueueCapacity = 64;

// Event ring capacity and per-pump drain bound. The ring holds bursts (FL fired
// FPD_ContextInfoChanged x29 in one short window - VERDICT Q3) between pumps;
// the drain writes at most this many lines per pump so one GUI pump is never a
// long file write. Anything past the bound drains on the next pump.
constexpr unsigned kEventRingCapacity = 4096;
constexpr std::size_t kMaxEventsPerPump = 512;

// The state dump is the only checkpoint that costs real work (two spool reads),
// so it stays off unless a human asks for it: `touch <spool>/bridge_debug.enable`
// enables it mid-session and deleting the file disables it, neither of which
// needs FL relaunched (an env var would - FL is launched from Finder).
constexpr char kDebugFlagName[] = "bridge_debug.enable";
constexpr int kDebugEveryNPumps = 100;

// How many Bridge instances are live in this process. Two instances sharing one
// ledger is a candidate explanation for the first-insert anomaly (a freshly
// menu-inserted instance consumed nothing, while the same dylib auto-loaded
// from the saved project consumed immediately), so it is worth one counter.
std::atomic<int> gLiveInstances{0};

// Which binary is this instance living in? If FL 2026 ever hosts the plugin in
// a separate helper process, that helper's HOME - and so its spool - can differ
// from the one the server writes. Cheap to record, and it settles that question
// in one live read.
std::string ExecutablePath() {
#if defined(__APPLE__)
  char path[1024];
  uint32_t size = sizeof(path);
  if (_NSGetExecutablePath(path, &size) == 0) return path;
#endif
  return "(unknown)";
}

// Two vDSO-backed clocks, safe to call from any thread including FL's mixing
// thread (the (GM) events): no lock, no allocation, no real syscall. The event
// ring stamps each push with both so the observer has a monotonic order and a
// wall-clock time without the tick having to guess either at drain time.
long long MonotonicNanos() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

long long UnixMillis() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

int ProcessId() {
#if defined(_WIN32)
  return static_cast<int>(GetCurrentProcessId());
#else
  return static_cast<int>(getpid());
#endif
}

std::string JoinIds(const std::vector<int> &ids) {
  std::string out;
  for (int id : ids) {
    if (!out.empty()) out += ",";
    out += std::to_string(id);
  }
  return out;
}

// Everything the executor touches, co-owned by the plugin instance and by any
// GUI-thread pump still queued when that instance goes away. A pump therefore
// never dereferences a destroyed Bridge: the lifetime hazard the marshal would
// otherwise introduce does not exist.
struct TickState : std::enable_shared_from_this<TickState> {
  TFruityPlugHost *host = nullptr;
  std::intptr_t host_tag = 0;
  std::unique_ptr<bridge_udp::UdpTransport> udp;
  bridge_core::ResultCache cache;  // GUI thread only, so unsynchronised

  // FL push events -> bridge_events.jsonl. The ring is filled from the
  // Dispatcher callback (any thread; some FPD_* are (GM)) and drained by the
  // GUI-thread pump into the log. `event_log` is null until the spool roots
  // resolve at construction; a null log just means events accumulate and drop
  // in the ring, never a crash.
  bridge_events::EventRing events{kEventRingCapacity};
  std::unique_ptr<bridge_events::EventLog> event_log;
  // FL's current pattern, learned from our own writes' SetCurrentPattern side
  // effect and reset to -1 (unknown) on FPD_ProjLoaded. Published as heartbeat
  // `pat=` for L1's detect(); there is no host getter for it (VERDICT Q2).
  std::atomic<int> current_pattern{-1};

  std::atomic<bool> alive{true};
  std::atomic<bool> pump_queued{false};
  std::atomic<long long> pumps_requested{0};
  std::atomic<long long> pumps_ran{0};
  std::atomic<long long> debug_pumps{0};  // pumps counted only while the debug flag is on
  // `applied` counts COMMANDS handled, not notes: a consumed ping writes no
  // notes, and liveness is exactly what a ping is asking about. Notes get their
  // own counter rather than overloading this one.
  std::atomic<int> commands_applied{0};
  std::atomic<int> notes_written{0};
  bridge_abi::Handshake abi;
  std::atomic<long long> render_blocks{0};
  std::atomic<long long> last_heartbeat_render_blocks{0};
  std::atomic<long long> last_heartbeat_ns{0};
  bridge_capture::Capture capture;

  // Called from the tick, on whatever thread FL ticks us on. Never touches the
  // host. At most one pump is in flight, so a GUI thread that stalls can only
  // ever leave one queued - and `gui_pending` in the heartbeat says so.
  //
  // A pump that never runs costs nothing: the ledger claim lives inside
  // ExecuteCommand, inside Pump, so a request that is dropped (FL quits, the
  // instance goes away) has claimed no id and owes no response. The command is
  // still queued and still absent from the ledger, so the next pump - this
  // session or the next - picks it up unchanged. The marshal therefore adds no
  // claimed-but-lost window; claim and response remain the same two
  // back-to-back statements on one thread they always were.
  void RequestPump();
  // GUI thread only. Drains both carriers through the one executor.
  void Pump();
  void Count(const bridge_core::PollOutcome &outcome);
};

void WriteHeartbeat(TickState &state, long long tick) {
  const long long now_ns = MonotonicNanos();
  const long long previous_ns = state.last_heartbeat_ns.exchange(now_ns);
  const long long blocks = state.render_blocks.load();
  const long long previous_blocks = state.last_heartbeat_render_blocks.exchange(blocks);
  bridge_core::HeartbeatState hb;
  hb.tick = tick;
  hb.applied = state.commands_applied.load();
  hb.notes = state.notes_written.load();
  hb.pid = ProcessId();
  hb.unix_time = (long long)time(nullptr);
  hb.udp_port = state.udp != nullptr ? state.udp->port() : 0;
  hb.udp_dropped = state.udp != nullptr ? (long long)state.udp->queue().dropped() : 0;
  hb.gui_pumps = state.pumps_ran.load();
  hb.gui_pending = state.pumps_requested.load() - state.pumps_ran.load();
  hb.tick_on_gui = bridge_gui::OnGuiThread() ? 1 : 0;
  hb.instances = gLiveInstances.load();
  hb.proto = bridge_core::kProtoVersion;
  hb.pattern = state.current_pattern.load();
  hb.channel = -1;  // no FL selected-channel getter exists (VERDICT Q2); always unknown
  hb.event_seq = (long long)state.events.last_seq();
  hb.event_dropped = (long long)state.events.dropped();
  hb.abi = state.abi.heartbeat_abi();
  hb.render_blocks = blocks;
  hb.render_rate = previous_ns > 0 && now_ns > previous_ns
                       ? (blocks - previous_blocks) * 1000000000LL / (now_ns - previous_ns)
                       : 0;
  hb.capture = state.capture.active() ? 1 : 0;
  hb.capture_frames = static_cast<long long>(state.capture.frames_written());
  WriteSmall("bridge_heartbeat.txt", bridge_core::FormatHeartbeat(hb).c_str());
}

// The last host call we entered, and whether FL gave it back. Always on: it is
// bounded by command rate (not tick rate), and a host call that never returns
// destroys every other kind of evidence - no crash report, no response file, no
// stack. A checkpoint left reading ":enter" names the call that hung.
void WriteHostCallTrace(const char *phase, int id, int notes) {
  char text[256];
  snprintf(text, sizeof(text), "phase=%s\nid=%d\nnotes=%d\ngui_thread=%d\nunix_time=%lld\n",
           phase, id, notes, bridge_gui::OnGuiThread() ? 1 : 0, (long long)time(nullptr));
  WriteSmall("bridge_dispatch.txt", text);
}

bool DebugEnabled() {
  if (gSpoolDir[0] == '\0') return false;
  char path[600];
  snprintf(path, sizeof(path), "%s/%s", gSpoolDir, kDebugFlagName);
  struct stat unused;
  return stat(path, &unused) == 0;
}

// What the poll actually sees, from inside the plugin: the roots it resolved,
// the ids it believes are pending, the ids the ledger says are settled, and the
// process identity behind all of it. Answers "the spool looks right from
// outside, so why is nothing consumed?" without a debugger.
void WriteDebugDump(const TickState &state) {
  const char *home = getenv("HOME");
  std::string text = "pid=" + std::to_string(ProcessId());
  text += "\nexe=" + ExecutablePath();
  text += std::string("\nHOME=") + (home != nullptr ? home : "(null)");
  text += "\ninstances=" + std::to_string(gLiveInstances.load());
  text += "\ngui_thread=" + std::to_string(bridge_gui::OnGuiThread() ? 1 : 0);
  text += "\nudp_port=" + std::to_string(state.udp != nullptr ? state.udp->port() : 0);
  text += "\nroots=" + std::to_string(gSpoolRoots.size());
  for (const std::string &root : gSpoolRoots) text += "\nroot=" + root;
  text += "\npending=" + JoinIds(bridge_core::QueueIds(gSpoolRoots));
  text += "\nconsumed=" + JoinIds(bridge_core::ReadConsumedIds(gSpoolRoots));
  text += "\n";
  WriteSmall("bridge_debug.txt", text.c_str());
}

void TickState::Count(const bridge_core::PollOutcome &outcome) {
  if (!outcome.had_command) return;
  ++commands_applied;
  notes_written += outcome.notes_written;
  // A note write switched FL's current pattern; track it for the heartbeat.
  if (outcome.pattern_after >= 1) current_pattern.store(outcome.pattern_after);
}

void TickState::RequestPump() {
  if (pump_queued.exchange(true)) return;
  ++pumps_requested;
  std::shared_ptr<TickState> self = shared_from_this();
  bridge_gui::RunOnGuiThread([self] {
    if (self->alive.load()) self->Pump();
    self->pump_queued.store(false);
  });
}

void TickState::Pump() {
  if (!abi.allows_host_dispatch()) return;
  ++pumps_ran;
  try {
    // UDP first (the latency path), then the file spool (the fallback). Both
    // feed the one executor and one ledger; only the carrier differs.
    if (udp != nullptr) {
      for (bridge_core::InboundDatagram &datagram : udp->queue().Drain(kMaxUdpCommandsPerPump)) {
        bridge_core::ExecResult result = bridge_core::ExecuteCommand(
            host, host_tag, gSpoolRoots, bridge_core::kDefaultPPQ, datagram.payload, &cache, &capture);
        udp->SendAck(datagram.reply_to, result.ack_json);
        Count(result.outcome);
      }
    }
    Count(bridge_core::PollOnce(host, host_tag, gSpoolRoots, bridge_core::kDefaultPPQ, &cache, &capture));
    // Drain forwarded FL events after the command drain (same thread discipline
    // as the transport: the ring is the only cross-thread surface, the write is
    // GUI-thread only). A null log means the roots never resolved; events then
    // just accumulate and drop in the ring, harmlessly.
    if (event_log != nullptr) {
      event_log->AppendBatch(events.Drain(kMaxEventsPerPump));
    }
    if (DebugEnabled() && debug_pumps++ % kDebugEveryNPumps == 0) WriteDebugDump(*this);
  } catch (...) {
    // This runs off FL's run loop, where an escaping exception kills the
    // process; the tick's own catch cannot cover it.
    WriteStatus("pump_exception");
  }
}

char gLongName[] = "FL MCP Bridge";
char gShortName[] = "MCPBR";

// #pragma pack(4) in the SDK puts LongName at offset 4; arm64 cannot emit a
// pointer relocation at a 4-byte-aligned address, so the name pointers are
// assigned at runtime rather than in this static initializer.
TFruityPlugInfo gPlugInfo = {
    CurrentSDKVersion,
    nullptr,  // LongName  - set in InitPlugInfo()
    nullptr,  // ShortName - set in InitPlugInfo()
    FPF_Type_Effect,  // effect (not generator): it lives on a mixer insert and
                      // owns no channel, so every write names TNotesParams.ChanNum
    0,  // NumParams
    0,  // DefPoly
    0,  // NumOutCtrls
    0,  // NumOutVoices
    {0},
};

void InitPlugInfo() {
  gPlugInfo.LongName = gLongName;
  gPlugInfo.ShortName = gShortName;
}

}  // namespace

// ABI-exact reimplementation of TFruityPlug. See the file header and
// native/spike1/NOBASE_FINDINGS.md for why this is not derived from the SDK
// class. Layout is static_asserted against the SDK header below.
class Bridge {
 public:
  // --- data: mirrors TFruityPlug's field order/types exactly -----------------
  TPluginTag HostTag = 0;
  PFruityPlugInfo Info = nullptr;
  HWND EditorHandle = 0;
  BOOL MonoRender = 0;
  int Reserved[32] = {0};
  // FL 2026's real TFruityPlug is larger than the public header; this zeroed
  // tail absorbs any host-side state it reads/writes past the classic fields.
  unsigned char ExtReserved[256] = {0};

  Bridge(TPluginTag tag, TFruityPlugHost *host);

  // --- vtable, laid out for FL 2026's FPC VMT indexing -----------------------
  //
  // FL is Free Pascal and indexes this object as an FPC class: a class's first
  // DECLARED method sits at VMT byte 200, past the header FPC puts in front
  // (native/abi/fl_host_abi.h). So the SDK's virtuals must start at C++ vtable
  // slot 25, not slot 0, or every host->plugin call lands on the wrong method.
  //
  // Slots 0..24 stand in for that header. FL never calls them (it only ever
  // indexes from byte 200), and it has never been observed READING them either
  // - it does not ask our object for its ClassName or InstanceSize. They are
  // pads so that if it ever does, the read is inside memory we own.
#define BRIDGE_VMT_HEADER_SLOT(n)                                             \
  virtual intptr_t _stdcall VmtHeader##n(intptr_t a, intptr_t b, intptr_t c) { \
    LogExtSlot(n, a, b, c);                                                   \
    return 0;                                                                 \
  }
  BRIDGE_VMT_HEADER_SLOT(0) BRIDGE_VMT_HEADER_SLOT(1) BRIDGE_VMT_HEADER_SLOT(2)
  BRIDGE_VMT_HEADER_SLOT(3) BRIDGE_VMT_HEADER_SLOT(4) BRIDGE_VMT_HEADER_SLOT(5)
  BRIDGE_VMT_HEADER_SLOT(6) BRIDGE_VMT_HEADER_SLOT(7) BRIDGE_VMT_HEADER_SLOT(8)
  BRIDGE_VMT_HEADER_SLOT(9) BRIDGE_VMT_HEADER_SLOT(10) BRIDGE_VMT_HEADER_SLOT(11)
  BRIDGE_VMT_HEADER_SLOT(12) BRIDGE_VMT_HEADER_SLOT(13) BRIDGE_VMT_HEADER_SLOT(14)
  BRIDGE_VMT_HEADER_SLOT(15) BRIDGE_VMT_HEADER_SLOT(16) BRIDGE_VMT_HEADER_SLOT(17)
  BRIDGE_VMT_HEADER_SLOT(18) BRIDGE_VMT_HEADER_SLOT(19) BRIDGE_VMT_HEADER_SLOT(20)
  BRIDGE_VMT_HEADER_SLOT(21) BRIDGE_VMT_HEADER_SLOT(22) BRIDGE_VMT_HEADER_SLOT(23)
  BRIDGE_VMT_HEADER_SLOT(24)
#undef BRIDGE_VMT_HEADER_SLOT

  // --- the SDK's TFruityPlug virtuals, in SDK declaration order --------------
  // C++ slot 25 == VMT byte 200 == the FPC "first declared method". Every entry
  // below is therefore the method FL actually calls at that offset, verified
  // live for 200/208/216/240/256 (see BRIDGE_FINDINGS.md).
  virtual void _stdcall DestroyObject();                                              // 25 / +200
  virtual intptr_t _stdcall Dispatcher(intptr_t id, intptr_t index, intptr_t value);  // 26 / +208
  virtual void _stdcall Idle_Public();                                                // 27 / +216
  virtual void _stdcall SaveRestoreState(IStream *stream, BOOL save);                 // 28 / +224
  virtual void _stdcall GetName(int section, int index, int value, char *name);       // 29 / +232
  virtual int _stdcall ProcessEvent(int event_id, int event_value, int flags);        // 30 / +240
  virtual int _stdcall ProcessParam(int index, int value, int rec_flags);             // 31 / +248
  virtual void _stdcall Eff_Render(PWAV32FS source, PWAV32FS dest, int length);       // 32 / +256
  virtual void _stdcall Gen_Render(PWAV32FS dest, int &length);                       // 33
  virtual TVoiceHandle _stdcall TriggerVoice(PVoiceParams voice_params, intptr_t set_tag);  // 34
  virtual void _stdcall Voice_Release(TVoiceHandle handle);                           // 35
  virtual void _stdcall Voice_Kill(TVoiceHandle handle);                              // 36
  virtual int _stdcall Voice_ProcessEvent(TVoiceHandle handle, intptr_t event_id,
                                          intptr_t event_value, intptr_t flags);      // 37
  virtual int _stdcall Voice_Render(TVoiceHandle handle, PWAV32FS dest, int &length);  // 38
  virtual void _stdcall NewTick();                                                    // 39
  virtual void _stdcall MIDITick();                                                   // 40
  virtual void _stdcall MIDIIn(int &msg);                                             // 41
  virtual void _stdcall MsgIn(intptr_t msg);                                          // 42
  virtual int _stdcall OutputVoice_ProcessEvent(TOutVoiceHandle handle, intptr_t event_id,
                                                intptr_t event_value, intptr_t flags);  // 43
  virtual void _stdcall OutputVoice_Kill(TVoiceHandle handle);                        // 44

  // --- tail pads, slots 45..63 -----------------------------------------------
  // The SDK declares 20 virtuals; FL 2026 has never been seen calling past
  // +256. These keep the vtable 64 slots long so a call to an offset we have
  // not characterised lands on a defined method that returns 0 instead of
  // running off the end into the adjacent RTTI - the run-11 crash. NO virtual
  // destructor exists anywhere in this class ON PURPOSE: implicit dtor slots
  // would shift every offset above them.
#define BRIDGE_PAD_SLOT(n)                                                    \
  virtual intptr_t _stdcall ExtSlot##n(intptr_t a, intptr_t b, intptr_t c) {  \
    LogExtSlot(n, a, b, c);                                                   \
    return 0;                                                                 \
  }
  BRIDGE_PAD_SLOT(45)
  BRIDGE_PAD_SLOT(46) BRIDGE_PAD_SLOT(47) BRIDGE_PAD_SLOT(48) BRIDGE_PAD_SLOT(49)
  BRIDGE_PAD_SLOT(50) BRIDGE_PAD_SLOT(51) BRIDGE_PAD_SLOT(52) BRIDGE_PAD_SLOT(53)
  BRIDGE_PAD_SLOT(54) BRIDGE_PAD_SLOT(55) BRIDGE_PAD_SLOT(56) BRIDGE_PAD_SLOT(57)
  BRIDGE_PAD_SLOT(58) BRIDGE_PAD_SLOT(59) BRIDGE_PAD_SLOT(60) BRIDGE_PAD_SLOT(61)
  BRIDGE_PAD_SLOT(62) BRIDGE_PAD_SLOT(63)
#undef BRIDGE_PAD_SLOT

 private:
  void Tick();
  void RequestIdleAlways();
  std::atomic<bool> idle_requested_{false};

  // Non-virtual, private: only DestroyObject (`delete this`) destroys the
  // object, exactly the SDK contract - and non-virtual keeps dtor slots out of
  // the vtable entirely.
  ~Bridge() = default;

  int tick_counter_ = 0;
  const long long constructed_ns_ = MonotonicNanos();
  long long heartbeat_tick_ = 0;
  // The live half of the plugin: host pointer, UDP transport, ack cache and
  // counters, shared with any GUI-thread pump this instance queued so the pump
  // can outlive the instance. Never null - allocation failure would throw out
  // of CreatePlugInstance instead, so the tick needs no null check. Members sit
  // after the ExtReserved tail, outside the host-visible layout.
  std::shared_ptr<TickState> state_ = std::make_shared<TickState>();
};

// Layout contract with the fetched SDK header: if it ever disagrees, fail the
// build, not FL Studio. (offsetof on this non-standard-layout class is
// conditionally-supported; clang evaluates it correctly and only warns.)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Winvalid-offsetof"
static_assert(offsetof(Bridge, HostTag) == offsetof(TFruityPlug, HostTag), "HostTag offset");
static_assert(offsetof(Bridge, Info) == offsetof(TFruityPlug, Info), "Info offset");
static_assert(offsetof(Bridge, EditorHandle) == offsetof(TFruityPlug, EditorHandle),
              "EditorHandle offset");
static_assert(offsetof(Bridge, MonoRender) == offsetof(TFruityPlug, MonoRender), "MonoRender offset");
static_assert(offsetof(Bridge, Reserved) == offsetof(TFruityPlug, Reserved), "Reserved offset");
static_assert(sizeof(((Bridge *)nullptr)->Reserved) == sizeof(((TFruityPlug *)nullptr)->Reserved),
              "Reserved size");
#pragma clang diagnostic pop

Bridge::Bridge(TPluginTag tag, TFruityPlugHost *host) {
  try {
    ++gLiveInstances;
    BuildSpoolState();
    WriteStatus("constructing");
    InitPlugInfo();
    Info = &gPlugInfo;
    HostTag = tag;
    EditorHandle = 0;
    state_->host = host;
    state_->host_tag = tag;
    bridge_core::SetHostCallTrace(WriteHostCallTrace);
    // This must remain the first host call. The palette-count query is a
    // read-only SDK operation with a bounded non-zero result; a wrong VMT
    // offset or incompatible host fails closed before any mutation is possible.
    state_->abi.Begin(fl_abi::HostDispatch(state_->host, state_->host_tag,
                                           FHD_GetColorRGBValue, 0, -1));
    if (state_->abi.status() == bridge_abi::Status::kMismatch) {
      WriteStatus((std::string("abi_mismatch:") + state_->abi.detail()).c_str());
      WriteHeartbeat(*state_, 0);
      return;
    }
    // The tick is Idle_Public, and FL delivers it only while the plugin's UI is
    // visible unless the plugin asks for "always" (fp_plugclass.h: FHD_WantIdle,
    // Value 2). This plugin has no UI, so a project saved with its wrapper
    // window closed never ticked at all: live runs 1-8 on 2026-09-04 showed
    // tick=0 for whole matches, unlocked, focused, App Nap disabled. The
    // earlier "no FHD_WantIdle needed" claim held only while a wrapper window
    // happened to be open. Construction is (G); the request is idempotent and
    // repeated from Dispatcher on the GUI thread in case a host ignores it here.
    RequestIdleAlways();
    // A failed UDP start (no urandom, no writable root) leaves udp null and
    // the plugin serves the file spool alone - the transport is an upgrade,
    // never a requirement.
    state_->udp = bridge_udp::UdpTransport::Start(gSpoolRoots, kUdpQueueCapacity);
    // The event log lives in every spool root (mirrors the response/heartbeat
    // discipline), so the server finds it in whichever root it resolves. Over-
    // sized logs are truncated here, at load, before the first append.
    std::vector<std::string> event_paths;
    event_paths.reserve(gSpoolRoots.size());
    for (const std::string &root : gSpoolRoots) {
      event_paths.push_back(root + "/" + bridge_events::kEventsFileName);
    }
    if (!event_paths.empty()) {
      state_->event_log = std::make_unique<bridge_events::EventLog>(std::move(event_paths));
    }
    WriteHeartbeat(*state_, 0);
    WriteStatus("abi_probing");
  } catch (...) {
    WriteStatus("constructor_exception");
  }
}

// Runs on whatever thread FL ticks us on - on FL 2026 that is the GUI thread,
// because the tick is `Idle_Public` (G). It still does NO host work itself: it
// asks for a GUI-thread pump and writes the heartbeat, so the property holds
// even if a future build ticks us from somewhere else. The heartbeat staying on
// this thread is deliberate - liveness keeps reporting even if the GUI thread is
// the thing that stalls, and `gui_pending` is then the number that says so.
void Bridge::RequestIdleAlways() {
  if (state_->abi.status() == bridge_abi::Status::kMismatch) return;
  if (idle_requested_.load()) return;
  if (!bridge_gui::OnGuiThread()) return;  // host calls stay on the GUI thread by construction
  try {
    fl_abi::HostDispatch(state_->host, state_->host_tag, FHD_WantIdle, 0, 2);
    idle_requested_.store(true);
    WriteStatus("idle_requested");
  } catch (...) {
    WriteStatus("want_idle_exception");
  }
}

void Bridge::Tick() {
  state_->abi.ObserveIdle();
  state_->abi.Evaluate((MonotonicNanos() - constructed_ns_) / 1000000000.0);
  if (state_->abi.status() == bridge_abi::Status::kMismatch) {
    WriteStatus((std::string("abi_mismatch:") + state_->abi.detail()).c_str());
    WriteHeartbeat(*state_, ++heartbeat_tick_);
    return;
  }
  if (!state_->abi.allows_host_dispatch()) {
    WriteHeartbeat(*state_, ++heartbeat_tick_);
    return;
  }
  if (++tick_counter_ < kPollEveryNTicks) return;
  tick_counter_ = 0;
  try {
    state_->RequestPump();
  } catch (...) {
    WriteStatus("tick_exception");
  }
  WriteHeartbeat(*state_, ++heartbeat_tick_);
}

void _stdcall Bridge::DestroyObject() {
  WriteStatus("destroyed");
  // A pump already queued on the GUI thread co-owns TickState and re-checks
  // `alive`, so it degrades to a no-op instead of a use-after-free - and a
  // no-op is exactly right, because it had claimed nothing yet (see
  // RequestPump). DestroyObject is (G) and so is the pump, so this can never
  // interleave with one mid-command; if a future FL broke that, the shared
  // ownership means the running pump still finishes and writes its response
  // rather than tearing down underneath itself.
  state_->alive.store(false);
  --gLiveInstances;
  // ~TickState runs ~UdpTransport, whose Stop() waits at most kJoinTimeoutMs
  // for the socket thread before detaching (docs/22 s2) - bounded no matter how
  // wedged the socket is.
  delete this;
}

// The SDK virtuals FL actually reaches. Silent no-ops matching the SDK
// defaults, except Idle_Public (the tick) and Eff_Render (passes audio through
// untouched, so this plugin is sonically inert).
intptr_t _stdcall Bridge::Dispatcher(intptr_t id, intptr_t index, intptr_t value) {
  // VMT +208. Live, FL sends the ordinary FPD_* setup messages here
  // (FPD_SetBlockSize with 128, FPD_SetSampleRate with 44100). The Bridge needs
  // none of them to write notes, so everything but the MIDI-resolution query
  // answers 0 ("not handled") - the same answer the pad build returned when it
  // loaded and ran stably. Logged so a future need is visible.
  LogExtSlot(26, id, index, value);
  state_->abi.ObserveDispatcher(id, value);
  // Forward the subscribed push events to the ring (docs/27 s5-6). This runs on
  // whatever thread FL sends the message on - some FPD_* are (GM), i.e. the
  // mixing thread - so it must only stamp the lock-free ring, never touch the
  // socket, the log file, or the host. The GUI-thread pump drains it.
  const int msg = static_cast<int>(id);
  if (bridge_events::IsSubscribed(msg)) {
    state_->events.Push(msg, static_cast<int>(index), static_cast<int>(value),
                        bridge_gui::OnGuiThread() ? 1 : 0, MonotonicNanos(), UnixMillis());
  }
  // A new project invalidates the tracked current pattern: FL's selection is
  // whatever the loaded project had, which our writes have not set yet.
  if (id == FPD_ProjLoaded) state_->current_pattern.store(-1);
  if (id == FPD_SetSampleRate) state_->capture.SetSampleRate(static_cast<int>(value));
  if (id == FPD_SetPlaying) state_->capture.SetPlaying(value != 0);
  return id == FPD_UseIncreasedMIDIResolution ? 1 : 0;
}
void _stdcall Bridge::Idle_Public() {
  // VMT +216, and THE tick. This is the SDK's own (G) idle callback - FL 2026
  // drives it from the Cocoa main run loop, so the pump runs inline with no
  // queueing at all. FL only delivers it for a hidden UI after FHD_WantIdle=2
  // (RequestIdleAlways, in the constructor). Before the FPC VMT offsets
  // were understood this same callback was reached as an unnamed "slot 27".
  LogExtSlot(27, 0, 0, 0);
  Tick();
}
void _stdcall Bridge::SaveRestoreState(IStream *stream, BOOL save) {
  (void)stream;
  (void)save;
}
void _stdcall Bridge::GetName(int section, int index, int value, char *name) {
  (void)section;
  (void)index;
  (void)value;
  (void)name;
}
int _stdcall Bridge::ProcessEvent(int event_id, int event_value, int flags) {
  (void)event_id;
  (void)event_value;
  (void)flags;
  return 0;
}
int _stdcall Bridge::ProcessParam(int index, int value, int rec_flags) {
  (void)index;
  (void)rec_flags;
  return value;
}
void _stdcall Bridge::Eff_Render(PWAV32FS source, PWAV32FS dest, int length) {
  ++state_->render_blocks;
  // The SDK's WAV32FS is two consecutive float samples. Push is bounded SPSC
  // storage only; the writer thread owns the WAV and all blocking work.
  state_->capture.Push(reinterpret_cast<const float *>(source), length);
  if (source != dest && length > 0) memcpy(dest, source, sizeof(TWAV32FS) * (size_t)length);
}
void _stdcall Bridge::Gen_Render(PWAV32FS dest, int &length) {
  (void)dest;
  (void)length;
}
TVoiceHandle _stdcall Bridge::TriggerVoice(PVoiceParams voice_params, intptr_t set_tag) {
  (void)voice_params;
  (void)set_tag;
  return 0;
}
void _stdcall Bridge::Voice_Release(TVoiceHandle handle) { (void)handle; }
void _stdcall Bridge::Voice_Kill(TVoiceHandle handle) { (void)handle; }
int _stdcall Bridge::Voice_ProcessEvent(TVoiceHandle handle, intptr_t event_id,
                                        intptr_t event_value, intptr_t flags) {
  (void)handle;
  (void)event_id;
  (void)event_value;
  (void)flags;
  return 0;
}
int _stdcall Bridge::Voice_Render(TVoiceHandle handle, PWAV32FS dest, int &length) {
  (void)handle;
  (void)dest;
  (void)length;
  return 0;
}
void _stdcall Bridge::NewTick() {}
void _stdcall Bridge::MIDITick() {}
void _stdcall Bridge::MIDIIn(int &msg) { (void)msg; }
void _stdcall Bridge::MsgIn(intptr_t msg) { (void)msg; }
int _stdcall Bridge::OutputVoice_ProcessEvent(TOutVoiceHandle handle, intptr_t event_id,
                                              intptr_t event_value, intptr_t flags) {
  (void)handle;
  (void)event_id;
  (void)event_value;
  (void)flags;
  return 0;
}
void _stdcall Bridge::OutputVoice_Kill(TVoiceHandle handle) { (void)handle; }

// intptr_t tag (not int - an int param truncates FL's real tag on this build).
// Returns void*: FL treats the result as an opaque object pointer + vtable.
#if defined(_WIN32)
#define FLMCP_EXPORT __declspec(dllexport)
#else
#define FLMCP_EXPORT __attribute__((visibility("default")))
#endif

extern "C" FLMCP_EXPORT void *_stdcall
CreatePlugInstance(TFruityPlugHost *host, TPluginTag tag) {
  return new Bridge(tag, host);
}

// Second export in Image-Line's own two-export surface. Per the run-11
// post-mortem this export is the gate FL 2026 uses to take the extended-slot
// path - which the Bridge WANTS, since slot 27 (the tick) is its autonomous
// entry point.
extern "C" FLMCP_EXPORT void _stdcall SetExternalAppHandle(intptr_t handle) {
  (void)handle;
}
