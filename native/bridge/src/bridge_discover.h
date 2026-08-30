// wp_* discovery ops - the probe's live passes, folded into the Bridge.
//
// The throwaway probe (native/probe/, deleted in this PR) answered issue #68's
// live questions with four spool-driven passes: getters (which FHD_* getters
// read the controller surface), timing (can an observer poll affordably),
// target (does FHD_AddNotesToPR honour PatNum/ChanNum), sweep (does any
// undocumented dispatcher id hand back a note list / channel object). Those
// passes are re-homed here as `wp_*` ops that ride the Bridge's ONE executor
// (bridge_core::ExecuteCommand) over the same v2 spool + UDP carriers, so
// discovery is a normal claimed-once command instead of a second plugin with
// its own ABI shell, session handshake, and resumable tick loop (~-2000 LOC).
//
// The safety strategy is unchanged and is the bulkhead's, not a second one:
//   * The static fatal-id ledger (native/abi/fl_dispatch_ledger.h) is consulted
//     before every dispatcher call; documented ids (0-87) and the known fatals
//     (94/99, ...) are skipped by construction over ANY range.
//   * A guarded, zero-filled buffer backs every pointer-out getter, so a
//     null-store hazard (36/37/38) faults into memory we own, never FL's.
//   * Claim-before-execute (the executor's ledger) means a command that crashes
//     FL never replays. A per-id durable in-flight checkpoint lets a re-issued
//     command quarantine an id a prior run crashed on, so a crash STRICTLY
//     advances discovery instead of looping - the probe's crash-safety, kept.
//   * Every returned integer is passed through fl_pascal (fault-free reads +
//     FPC class identification) so a sweep reports named FL objects, never a
//     blind cast that would deref a garbage pointer.
//
// Results ride back in the command's ack JSON (responses/<id>.json), which is
// the durable evidence the analyzer/RUNBOOK reads - one place, per the
// executor's existing discipline.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "fp_plugclass.h"
#include "minijson.h"

namespace bridge_discover {

// The wp_* op names this module handles.
bool IsDiscoverOp(const std::string &op);

// A caller-allocated, zero-filled buffer with a trailing PROT_NONE guard page:
// a host write past `size` faults loudly instead of corrupting neighbours.
// This is the substrate for the pointer-out getters; reads back out are
// bounds-checked. Ported verbatim from the probe (native/probe probe_core).
class GuardedBuffer {
 public:
  explicit GuardedBuffer(std::size_t size);
  ~GuardedBuffer();
  GuardedBuffer(const GuardedBuffer &) = delete;
  GuardedBuffer &operator=(const GuardedBuffer &) = delete;

  void *data() const { return data_; }
  std::size_t size() const { return size_; }
  bool valid() const { return data_ != nullptr; }
  void Zero();
  void HexEncode(char *out, std::size_t out_size) const;

 private:
  void *region_ = nullptr;
  void *data_ = nullptr;
  std::size_t size_ = 0;
  std::size_t region_bytes_ = 0;
};

// One documented getter and how to call it safely - the ONLY place a documented
// buffer-out id is called, and always with its real calling convention.
struct GetterSpec {
  int id;
  const char *name;
  enum class Call {
    kIndexOnly,       // return value carries the answer; Value unused (0)
    kBufferInValue,   // Value = pointer to a zeroed guarded buffer to fill
    kReturnsPointer,  // return value is a host pointer; log it, never deref
  } call;
  std::intptr_t index;
  const char *note;
};

// The curated SAFE getter table (goal 2 + 3 of the probe verdict).
const std::vector<GetterSpec> &Getters();

// The MIDI key wp_target plants for step `step`: unique per step so a file
// readback attributes every landed note to one (PatNum, ChanNum) with no
// ambiguity. Exposed for the test.
int TargetStepKey(int step);

// Run one wp_* op. `args` is the command's parsed `args` object; `roots` are
// the spool roots (the first is where the durable in-flight checkpoint + the
// wp_quarantine ledger live). `host` may be null in tests, in which case ops
// that need a dispatcher return an error result. Returns the JSON object string
// for the ack's `result` field. Never throws.
std::string RunOp(TFruityPlugHost *host, std::intptr_t host_tag, const std::string &op,
                  const minijson::Value &args, const std::vector<std::string> &roots);

}  // namespace bridge_discover
