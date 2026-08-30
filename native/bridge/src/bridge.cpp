// FL MCP Bridge — the production note-writing plugin. Removes the human from
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
//     public header's 20 virtuals — its loader host-calls extended slots
//     (observed live: 26, 27, 30, 32; the RE found call sites for 25/26/31).
//     A 22-slot vtable let those calls run off the end into adjacent RTTI data
//     and branch there (the run-11 EBusError at typeinfo+0). Slots 20..63 are
//     real virtuals here, so every extended call lands on a defined method.
//   * ExtReserved object tail: FL reads a pointer out of the object's +0xa0
//     region on the extended path and may write host-side state past the
//     classic fields; this zeroed tail keeps such access inside memory we own.
//
// The note-writing and spool logic lives in bridge_core.* so it is unit-tested
// under ASan/UBSan against a mock host (bridge/tests/bridge_core_test.cpp) with
// no FL in the loop. This file is only the ABI shell + the extended-slot map.

#include <unistd.h>

#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <memory>
#include <string>
#include <vector>

#include "bridge_core.h"
#include "bridge_udp.h"
#include "fp_plugclass.h"

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
  if (home == nullptr) return;
  snprintf(gSpoolDir, sizeof(gSpoolDir),
           "%s/Documents/Image-Line/FL Studio/Settings/Piano roll scripts/FLMCP", home);
  gSpoolRoots = bridge_core::SpoolRoots(home);
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

void WriteHeartbeat(long long tick, int applied, int udp_port, long long udp_dropped) {
  char text[224];
  snprintf(text, sizeof(text),
           "tick=%lld\napplied=%d\npid=%d\nunix_time=%lld\nbackend=bridge\n"
           "udp_port=%d\nudp_dropped=%lld\n",
           tick, applied, (int)getpid(), (long long)time(nullptr), udp_port, udp_dropped);
  WriteSmall("bridge_heartbeat.txt", text);
}

// Extended-slot discovery log, capped per slot. Turns FL 2026's undocumented
// extended ABI into evidence instead of a crash, without growing without bound.
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
  fprintf(f, "ext slot=%d a=%lld b=%lld c=%lld\n", slot, (long long)a, (long long)b, (long long)c);
  fclose(f);
}

char gLongName[] = "FL MCP Bridge";
char gShortName[] = "MCPBR";

// #pragma pack(4) in the SDK puts LongName at offset 4; arm64 cannot emit a
// pointer relocation at a 4-byte-aligned address, so the name pointers are
// assigned at runtime rather than in this static initializer.
TFruityPlugInfo gPlugInfo = {
    CurrentSDKVersion,
    nullptr,  // LongName  — set in InitPlugInfo()
    nullptr,  // ShortName — set in InitPlugInfo()
    FPF_Type_Effect,  // effect (not generator): TNotesParams.ChanNum retargets to
                      // the selected channel only for effects (fp_plugclass.h)
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

// The extended tick (FL 2026 slot 27) fires often; polling the filesystem on
// every one is wasteful. Poll every Nth tick — still sub-second responsiveness.
constexpr int kPollEveryNTicks = 3;

// Bounded work per tick: a UDP burst must never turn one tick into a stall of
// FL's GUI thread (docs/22 s2). The rest of the burst drains on later ticks.
constexpr std::size_t kMaxUdpCommandsPerTick = 4;
constexpr std::size_t kUdpQueueCapacity = 64;

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

  // --- classic vtable, slots 0..19, SDK declaration order --------------------
  virtual void _stdcall DestroyObject();                                              // 0
  virtual intptr_t _stdcall Dispatcher(intptr_t id, intptr_t index, intptr_t value);  // 1
  virtual void _stdcall Idle_Public();                                                // 2
  virtual void _stdcall SaveRestoreState(IStream *stream, BOOL save);                 // 3
  virtual void _stdcall GetName(int section, int index, int value, char *name);       // 4
  virtual int _stdcall ProcessEvent(int event_id, int event_value, int flags);        // 5
  virtual int _stdcall ProcessParam(int index, int value, int rec_flags);             // 6
  virtual void _stdcall Eff_Render(PWAV32FS source, PWAV32FS dest, int length);       // 7
  virtual void _stdcall Gen_Render(PWAV32FS dest, int &length);                       // 8
  virtual TVoiceHandle _stdcall TriggerVoice(PVoiceParams voice_params, intptr_t set_tag);  // 9
  virtual void _stdcall Voice_Release(TVoiceHandle handle);                           // 10
  virtual void _stdcall Voice_Kill(TVoiceHandle handle);                              // 11
  virtual int _stdcall Voice_ProcessEvent(TVoiceHandle handle, intptr_t event_id,
                                          intptr_t event_value, intptr_t flags);      // 12
  virtual int _stdcall Voice_Render(TVoiceHandle handle, PWAV32FS dest, int &length);  // 13
  virtual void _stdcall NewTick();                                                    // 14
  virtual void _stdcall MIDITick();                                                   // 15
  virtual void _stdcall MIDIIn(int &msg);                                             // 16
  virtual void _stdcall MsgIn(intptr_t msg);                                          // 17
  virtual int _stdcall OutputVoice_ProcessEvent(TOutVoiceHandle handle, intptr_t event_id,
                                                intptr_t event_value, intptr_t flags);  // 18
  virtual void _stdcall OutputVoice_Kill(TVoiceHandle handle);                        // 19

  // --- extended-ABI slots 20..63 ---------------------------------------------
  // FL 2026 host-calls into this range (observed live: 26/27/30/32). Signatures
  // are undocumented, so each takes three integer args (harmless to read extra
  // arg registers on both AAPCS64 and x86_64 SysV) and returns 0 ("unsupported")
  // — except slot 27, the tick, which drives the spool poll. NO virtual
  // destructor exists anywhere in this class ON PURPOSE: implicit dtor slots
  // would sit at 20/21, where a host call would run `delete this`.
#define BRIDGE_PAD_SLOT(n)                                                    \
  virtual intptr_t _stdcall ExtSlot##n(intptr_t a, intptr_t b, intptr_t c) {  \
    LogExtSlot(n, a, b, c);                                                   \
    return 0;                                                                 \
  }
  BRIDGE_PAD_SLOT(20) BRIDGE_PAD_SLOT(21) BRIDGE_PAD_SLOT(22) BRIDGE_PAD_SLOT(23)
  BRIDGE_PAD_SLOT(24) BRIDGE_PAD_SLOT(25)
  // slot 26: a host query (observed a=query-id, c=value, e.g. a=4/c=44100
  // samplerate). We do not need any of these to write notes, so the sane
  // response is 0 ("not handled") — the same value the pure-pad build returned
  // when it loaded and ran stably. Logged for future refinement.
  virtual intptr_t _stdcall ExtSlot26(intptr_t a, intptr_t b, intptr_t c) {
    LogExtSlot(26, a, b, c);
    return 0;
  }
  // slot 27: the tick. FL calls this repeatedly while the plugin is live; it is
  // our autonomous entry point (the base-free ABI never calls FHD_WantIdle, the
  // dispatcher call implicated in earlier crashes). Poll the spool here.
  virtual intptr_t _stdcall ExtSlot27(intptr_t a, intptr_t b, intptr_t c) {
    LogExtSlot(27, a, b, c);
    Tick();
    return 0;
  }
  BRIDGE_PAD_SLOT(28) BRIDGE_PAD_SLOT(29)
  BRIDGE_PAD_SLOT(30) BRIDGE_PAD_SLOT(31) BRIDGE_PAD_SLOT(32) BRIDGE_PAD_SLOT(33)
  BRIDGE_PAD_SLOT(34) BRIDGE_PAD_SLOT(35) BRIDGE_PAD_SLOT(36) BRIDGE_PAD_SLOT(37)
  BRIDGE_PAD_SLOT(38) BRIDGE_PAD_SLOT(39) BRIDGE_PAD_SLOT(40) BRIDGE_PAD_SLOT(41)
  BRIDGE_PAD_SLOT(42) BRIDGE_PAD_SLOT(43) BRIDGE_PAD_SLOT(44) BRIDGE_PAD_SLOT(45)
  BRIDGE_PAD_SLOT(46) BRIDGE_PAD_SLOT(47) BRIDGE_PAD_SLOT(48) BRIDGE_PAD_SLOT(49)
  BRIDGE_PAD_SLOT(50) BRIDGE_PAD_SLOT(51) BRIDGE_PAD_SLOT(52) BRIDGE_PAD_SLOT(53)
  BRIDGE_PAD_SLOT(54) BRIDGE_PAD_SLOT(55) BRIDGE_PAD_SLOT(56) BRIDGE_PAD_SLOT(57)
  BRIDGE_PAD_SLOT(58) BRIDGE_PAD_SLOT(59) BRIDGE_PAD_SLOT(60) BRIDGE_PAD_SLOT(61)
  BRIDGE_PAD_SLOT(62) BRIDGE_PAD_SLOT(63)
#undef BRIDGE_PAD_SLOT

 private:
  void Tick();

  // Non-virtual, private: only DestroyObject (`delete this`) destroys the
  // object, exactly the SDK contract — and non-virtual keeps dtor slots out of
  // the vtable entirely.
  ~Bridge() = default;

  TFruityPlugHost *host_ = nullptr;
  int tick_counter_ = 0;
  long long heartbeat_tick_ = 0;
  int applied_total_ = 0;
  // UDP transport + the shared ack cache (docs/22). Members sit after the
  // ExtReserved tail like the counters above, outside the host-visible layout.
  bridge_core::ResultCache result_cache_;
  std::unique_ptr<bridge_udp::UdpTransport> udp_;
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
    BuildSpoolState();
    WriteStatus("constructing");
    InitPlugInfo();
    Info = &gPlugInfo;
    HostTag = tag;
    EditorHandle = 0;
    host_ = host;
    // Deliberately no FHD_WantIdle: the base-free ABI gets its autonomous
    // callback from the extended tick (slot 27), and FHD_WantIdle was the
    // dispatcher call implicated in the earlier crash chain (run 5).
    // A failed UDP start (no urandom, no writable root) leaves udp_ null and
    // the plugin serves the file spool alone - the transport is an upgrade,
    // never a requirement.
    udp_ = bridge_udp::UdpTransport::Start(gSpoolRoots, kUdpQueueCapacity);
    WriteHeartbeat(0, 0, udp_ != nullptr ? udp_->port() : 0, 0);
    WriteStatus("loaded");
  } catch (...) {
    WriteStatus("constructor_exception");
  }
}

void Bridge::Tick() {
  if (++tick_counter_ < kPollEveryNTicks) return;
  tick_counter_ = 0;
  try {
    // UDP first (the latency path), then the file spool (the fallback). Both
    // feed the one executor and one ledger; only the carrier differs.
    if (udp_ != nullptr) {
      for (bridge_core::InboundDatagram &datagram : udp_->queue().Drain(kMaxUdpCommandsPerTick)) {
        bridge_core::ExecResult result =
            bridge_core::ExecuteCommand(host_, HostTag, gSpoolRoots, bridge_core::kDefaultPPQ,
                                        datagram.payload, &result_cache_);
        udp_->SendAck(datagram.reply_to, result.ack_json);
        if (result.outcome.had_command && result.outcome.ok) {
          applied_total_ += result.outcome.notes_written;  // pings contribute 0
        }
      }
    }
    bridge_core::PollOutcome out = bridge_core::PollOnce(host_, HostTag, gSpoolRoots,
                                                         bridge_core::kDefaultPPQ, &result_cache_);
    if (out.had_command) applied_total_ += out.ok ? out.notes_written : 0;
  } catch (...) {
    WriteStatus("tick_exception");
  }
  WriteHeartbeat(++heartbeat_tick_, applied_total_, udp_ != nullptr ? udp_->port() : 0,
                 udp_ != nullptr ? (long long)udp_->queue().dropped() : 0);
}

void _stdcall Bridge::DestroyObject() {
  WriteStatus("destroyed");
  // ~Bridge runs ~UdpTransport, whose Stop() waits at most kJoinTimeoutMs for
  // the socket thread before detaching (docs/22 s2) - this (G)-thread call is
  // bounded no matter how wedged the socket is.
  delete this;
}

// Classic slots: silent no-ops matching the SDK defaults. Eff_Render passes
// audio through untouched (this plugin is sonically inert). None of these are
// the note path — that runs on the tick.
intptr_t _stdcall Bridge::Dispatcher(intptr_t id, intptr_t index, intptr_t value) {
  (void)index;
  (void)value;
  return id == FPD_UseIncreasedMIDIResolution ? 1 : 0;
}
void _stdcall Bridge::Idle_Public() {
  // If FL ever calls the classic idle unsolicited, use it as a poll source too
  // (harmless; shares the same throttle path). Primary driver is slot 27.
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

// intptr_t tag (not int — an int param truncates FL's real tag on this build).
// Returns void*: FL treats the result as an opaque object pointer + vtable.
extern "C" __attribute__((visibility("default"))) void *_stdcall
CreatePlugInstance(TFruityPlugHost *host, TPluginTag tag) {
  return new Bridge(tag, host);
}

// Second export in Image-Line's own two-export surface. Per the run-11
// post-mortem this export is the gate FL 2026 uses to take the extended-slot
// path — which the Bridge WANTS, since slot 27 (the tick) is its autonomous
// entry point.
extern "C" __attribute__((visibility("default"))) void _stdcall SetExternalAppHandle(intptr_t handle) {
  (void)handle;
}
