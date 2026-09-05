// When L2's generated profile is present, ITS struct-assert wall (pinned from
// FL's own record RTTI by the scraper) replaces the hand-written wall below.
// This TU materializes it: FLMCP_ASSERT_STRUCTS must be defined before the
// first include of fl_profile_generated.h, which bridge_core.h performs right
// after the SDK plugin API header.
#if __has_include("fl_profile_generated.h")
#define FLMCP_ASSERT_STRUCTS 1
#endif

#include "bridge_core.h"

#if defined(_WIN32)
#include <direct.h>
#else
#include <sys/stat.h>
#endif

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "bridge_discover.h"
#include "fl_host_abi.h"
#include FL_MCP_PLUGIN_API_HEADER

namespace bridge_core {

namespace {

std::string Join(const std::string &dir, const char *name) { return dir + "/" + name; }

// The server's zero-padded per-command file name (spool.py f"{id:06d}.json").
std::string CommandName(int id) {
  char name[32];
  snprintf(name, sizeof(name), "%06d.json", id);
  return name;
}

bool ReadFile(const std::string &path, std::string *out) {
  FILE *f = fopen(path.c_str(), "rb");
  if (f == nullptr) return false;
  out->clear();
  char buffer[4096];
  size_t n;
  while ((n = fread(buffer, 1, sizeof(buffer), f)) > 0) out->append(buffer, n);
  fclose(f);
  return true;
}

bool WriteFile(const std::string &path, const std::string &text) {
  FILE *f = fopen(path.c_str(), "wb");
  if (f == nullptr) return false;
  size_t written = fwrite(text.data(), 1, text.size(), f);
  fclose(f);
  return written == text.size();
}

// Pending ids listed by any root's queue index. The queue - never a directory
// listing - is the discovery mechanism, matching the pyscript (directory
// scanning is unmeasured in FL's sandbox; here it is just protocol fidelity).
bool ReadQueue(const std::string &root, std::vector<int> *out) {
  std::string text;
  if (!ReadFile(Join(root, "commands/queue.json"), &text)) return false;
  minijson::Value doc;
  if (!minijson::Parse(text.c_str(), &doc)) return false;
  const minijson::Value *list = doc.Get("ids");
  if (list == nullptr || list->type != minijson::Type::Array) return false;
  for (const minijson::Value &item : list->array) {
    if (item.type == minijson::Type::Number) out->push_back(static_cast<int>(item.number));
  }
  return true;
}

// First readable non-tombstone copy of the command across roots (the mirrored
// copies are equivalent; a tombstone anywhere means settled only when no root
// still holds the real command - same rule as the pyscript's _read_command).
// Returns the raw text so the executor receives the command JSON verbatim,
// exactly as the UDP carrier delivers it.
bool ReadCommand(const std::vector<std::string> &roots, int id, std::string *out_text) {
  for (const std::string &root : roots) {
    std::string text;
    if (!ReadFile(Join(root, ("commands/" + CommandName(id)).c_str()), &text)) continue;
    minijson::Value parsed;
    if (!minijson::Parse(text.c_str(), &parsed)) continue;
    if (parsed.type != minijson::Type::Object) continue;
    const minijson::Value *op = parsed.Get("op");
    if (op == nullptr || op->type != minijson::Type::String) continue;  // tombstone
    *out_text = text;
    return true;
  }
  return false;
}

void WriteResponse(const std::vector<std::string> &roots, int id, const std::string &json) {
  for (const std::string &root : roots) {
    // The server mkdirs responses/ at Spool construction; recreate it here so a
    // half-provisioned root still gets its mirror copy (native code, no sandbox).
#if defined(_WIN32)
    _mkdir(Join(root, "responses").c_str());
#else
    mkdir(Join(root, "responses").c_str(), 0755);
#endif
    WriteFile(Join(root, ("responses/" + CommandName(id)).c_str()), json);
  }
}

bool Contains(const std::vector<int> &ids, int id) {
  return std::find(ids.begin(), ids.end(), id) != ids.end();
}

// SpoolResponse.op is a closed literal in schemas.py; an unexpected string
// would fail the server's validation, so anything else is reported as op-less.
bool IsKnownOp(const std::string &op) {
  return op == "add_notes" || op == "clear_notes" || op == "ping" ||
         op == "capture_start" || op == "capture_stop";
}

HostCallTrace g_host_call_trace = nullptr;

void Trace(const char *phase, int id, int notes) {
  if (g_host_call_trace != nullptr) g_host_call_trace(phase, id, notes);
}

}  // namespace

void SetHostCallTrace(HostCallTrace trace) { g_host_call_trace = trace; }

std::string FormatHeartbeat(const HeartbeatState &s) {
  // Fixed key set and order: the first block is the historical heartbeat the
  // existing tools read; proto/pat/chan are the docs/27 s6 additions L1's
  // detect() parses. pat/chan are -1 when unknown.
  std::string out;
  out += "tick=" + std::to_string(s.tick) + "\n";
  out += "applied=" + std::to_string(s.applied) + "\n";
  out += "notes=" + std::to_string(s.notes) + "\n";
  out += "pid=" + std::to_string(s.pid) + "\n";
  out += "unix_time=" + std::to_string(s.unix_time) + "\n";
  out += "backend=bridge\n";
  out += "udp_port=" + std::to_string(s.udp_port) + "\n";
  out += "udp_dropped=" + std::to_string(s.udp_dropped) + "\n";
  out += "gui_pumps=" + std::to_string(s.gui_pumps) + "\n";
  out += "gui_pending=" + std::to_string(s.gui_pending) + "\n";
  out += "tick_on_gui=" + std::to_string(s.tick_on_gui) + "\n";
  out += "instances=" + std::to_string(s.instances) + "\n";
  out += "proto=" + std::to_string(s.proto) + "\n";
  out += "pat=" + std::to_string(s.pattern) + "\n";
  out += "chan=" + std::to_string(s.channel) + "\n";
  out += "event_seq=" + std::to_string(s.event_seq) + "\n";
  out += "event_dropped=" + std::to_string(s.event_dropped) + "\n";
  out += std::string("abi=") + s.abi + "\n";
  out += "render_blocks=" + std::to_string(s.render_blocks) + "\n";
  out += "render_rate=" + std::to_string(s.render_rate) + "\n";
  out += "capture=" + std::to_string(s.capture) + "\n";
  out += "capture_frames=" + std::to_string(s.capture_frames) + "\n";
  return out;
}

std::vector<std::string> SpoolRoots(const char *home, const char *spool_root) {
  std::vector<std::string> roots;
  if (spool_root != nullptr && spool_root[0] != '\0') {
    roots.emplace_back(spool_root);
    return roots;
  }
  if (home == nullptr || home[0] == '\0') return roots;
  const char *subdirs[] = {"Piano roll scripts/FLMCP", "Piano roll scripts"};
  for (const char *sub : subdirs) {
    roots.push_back(std::string(home) + "/Documents/Image-Line/FL Studio/Settings/" + sub);
  }
  return roots;
}

// --- transport seam ----------------------------------------------------------

void CommandQueue::Push(InboundDatagram datagram) {
  std::lock_guard<std::mutex> lock(mu_);
  if (items_.size() >= capacity_) {
    items_.pop_front();
    ++dropped_;
  }
  items_.push_back(std::move(datagram));
}

std::vector<InboundDatagram> CommandQueue::Drain(std::size_t max_items) {
  std::vector<InboundDatagram> out;
  std::lock_guard<std::mutex> lock(mu_);
  while (!items_.empty() && out.size() < max_items) {
    out.push_back(std::move(items_.front()));
    items_.pop_front();
  }
  return out;
}

std::uint64_t CommandQueue::dropped() const {
  std::lock_guard<std::mutex> lock(mu_);
  return dropped_;
}

const std::string *ResultCache::Find(int id) const {
  for (const auto &entry : entries_) {
    if (entry.first == id) return &entry.second;
  }
  return nullptr;
}

void ResultCache::Put(int id, std::string ack_json) {
  if (entries_.size() >= capacity_) entries_.pop_front();
  entries_.emplace_back(id, std::move(ack_json));
}

// --- NotesParamsBuffer -------------------------------------------------------

NotesParamsBuffer::NotesParamsBuffer(NotesParamsBuffer &&other) noexcept
    : params(other.params),
      bytes(other.bytes),
      count(other.count),
      ppq(other.ppq),
      used_default_ppq(other.used_default_ppq),
      error(other.error) {
  other.params = nullptr;
  other.bytes = 0;
  other.count = 0;
}

NotesParamsBuffer &NotesParamsBuffer::operator=(NotesParamsBuffer &&other) noexcept {
  if (this != &other) {
    free(params);
    params = other.params;
    bytes = other.bytes;
    count = other.count;
    ppq = other.ppq;
    used_default_ppq = other.used_default_ppq;
    error = other.error;
    other.params = nullptr;
    other.bytes = 0;
    other.count = 0;
  }
  return *this;
}

NotesParamsBuffer::~NotesParamsBuffer() { free(params); }

// --- the FL 2026 note-params layout contract ---------------------------------
// Derived from FLEngine_x64.dylib's own FPC record RTTI (FL Studio 2026,
// 26.1.5 build 5384, arm64): TNoteParams is 32 bytes with fields at
// 0/4/8/12/16/18/20/24/28, TNotesParams is 52 bytes with the note array at 20.
// The fetched C++ header agrees on every offset, so pin them here: if it ever
// drifts, fail the build, not FL Studio. The single disagreement is the TYPE of
// the field at offset 12 (Single, not int), which SetNoteVol/NoteVol own.
//
// FALLBACK ONLY: when L2's generated profile is present its scraped assert
// wall (materialized at the top of this TU) is the single home for these
// facts, and this hand-written block compiles out (docs/27 s6).
#ifndef FLMCP_PROFILE_GENERATED
static_assert(sizeof(TNoteParams) == 32, "FL 2026 TNoteParams is 32 bytes");
static_assert(offsetof(TNoteParams, Position) == 0, "TNoteParams.Position @0");
static_assert(offsetof(TNoteParams, Length) == 4, "TNoteParams.Length @4");
static_assert(offsetof(TNoteParams, Pan) == 8, "TNoteParams.Pan @8");
static_assert(offsetof(TNoteParams, Vol) == 12, "TNoteParams.Vol @12");
static_assert(sizeof(((TNoteParams *)nullptr)->Vol) == sizeof(float), "TNoteParams.Vol is 4 bytes");
static_assert(offsetof(TNoteParams, Note) == 16, "TNoteParams.Note @16");
static_assert(offsetof(TNoteParams, Color) == 18, "TNoteParams.Color @18");
static_assert(offsetof(TNoteParams, Pitch) == 20, "TNoteParams.Pitch @20");
static_assert(offsetof(TNoteParams, FCut) == 24, "TNoteParams.FCut @24");
static_assert(offsetof(TNoteParams, FRes) == 28, "TNoteParams.FRes @28");
static_assert(offsetof(TNotesParams, Target) == 0, "TNotesParams.Target @0");
static_assert(offsetof(TNotesParams, Flags) == 4, "TNotesParams.Flags @4");
static_assert(offsetof(TNotesParams, PatNum) == 8, "TNotesParams.PatNum @8");
static_assert(offsetof(TNotesParams, ChanNum) == 12, "TNotesParams.ChanNum @12");
static_assert(offsetof(TNotesParams, Count) == 16, "TNotesParams.Count @16");
static_assert(offsetof(TNotesParams, NoteParams) == 20, "TNotesParams.NoteParams @20");
static_assert(sizeof(TNotesParams) == 52, "FL 2026 TNotesParams is 52 bytes with one note");
#endif  // !FLMCP_PROFILE_GENERATED

void SetNoteVol(TNoteParams *note, float vol) { std::memcpy(&note->Vol, &vol, sizeof(float)); }

float NoteVol(const TNoteParams &note) {
  float vol = 0.0f;
  std::memcpy(&vol, &note.Vol, sizeof(float));
  return vol;
}

NotesParamsBuffer BuildNotesParams(const minijson::Value &args, int ppq_default, bool empty_first) {
  NotesParamsBuffer buf;

  const minijson::Value *notes = args.Get("notes");
  int count = 0;
  if (notes != nullptr && notes->type == minijson::Type::Array) {
    count = static_cast<int>(notes->array.size());
  }
  if (count > kMaxNotesPerCommand) count = kMaxNotesPerCommand;

  // The v2 ppq stamp (schemas.AddNotesArgs.ppq). None serializes as JSON null,
  // so anything but a positive number falls back - flagged, so the response
  // summary can carry the warning.
  const minijson::Value *ppq_field = args.Get("ppq");
  buf.used_default_ppq =
      ppq_field == nullptr || ppq_field->type != minijson::Type::Number || ppq_field->number <= 0;
  const double ppq = buf.used_default_ppq ? static_cast<double>(ppq_default) : ppq_field->number;
  buf.ppq = static_cast<int>(ppq);

  const bool replace = empty_first || args.Str("mode", "merge") == "replace";

  // A concrete target or nothing - see the header for why -1 is not an option.
  const minijson::Value *target = args.Get("target");
  if (target == nullptr || target->type != minijson::Type::Object) {
    buf.error = "command carries no target object";
    return buf;
  }
  const int pat_num = target->Int("pattern", 0);
  const int chan_num = target->Int("channel", -1);
  if (pat_num < 1 || chan_num < 0) {
    buf.error = "target must name a concrete pattern (>= 1) and channel (>= 0)";
    return buf;
  }

  // TNoteParams is #pragma pack(1) and TNotesParams ends in a flexible array,
  // so size from offsetof, not sizeof (which would fold in the trailing
  // element). ONE SPARE ZEROED SLOT past Count, always: it gives a pure clear
  // (count 0 + replace) a valid NoteParams[0], and an inclusive
  // `for i := 0 to Count` on FL's side still lands in memory we own and
  // zeroed - the same medicine bridge.cpp's ExtReserved tail applies to the
  // object, for the same reason (FL 2026 reads past documented struct ends).
  const int alloc_slots = count + 1;
  size_t bytes = offsetof(TNotesParams, NoteParams) + static_cast<size_t>(alloc_slots) * sizeof(TNoteParams);
  TNotesParams *params = static_cast<TNotesParams *>(calloc(1, bytes));
  if (params == nullptr) {
    buf.error = "out of memory building notes";
    return buf;
  }

  params->Target = 1;  // piano roll
  params->Flags = replace ? NPF_EmptyFirst : 0;
  params->PatNum = pat_num;
  params->ChanNum = chan_num;
  params->Count = count;

  for (int i = 0; i < count; ++i) {
    const minijson::Value &n = notes->array[static_cast<size_t>(i)];
    TNoteParams *out = &params->NoteParams[i];
    // calloc already zeroed the struct.
    out->Position = static_cast<int>(n.Num("start", 0.0) * ppq + 0.5);
    int length = static_cast<int>(n.Num("length", 1.0) * ppq + 0.5);
    out->Length = length > 0 ? length : 1;
    out->Note = static_cast<short>(n.Int("key", 60));
    // Server velocity and FL's Vol are the same 0..1 scale (FL's default note
    // velocity, 100/128, is this field's documented default), so this is a
    // clamp, not a conversion - but Vol is a Single, hence SetNoteVol.
    const double vel = n.Num("velocity", 0.8);
    SetNoteVol(out, static_cast<float>(vel < 0.0 ? 0.0 : (vel > 1.0 ? 1.0 : vel)));
    // Server pan is 0..1 centred at 0.5; TNoteParams.Pan is centred at 0.
    double pan = (n.Num("pan", 0.5) - 0.5) * 128.0;
    out->Pan = static_cast<int>(pan + (pan >= 0 ? 0.5 : -0.5));
    out->Color = static_cast<short>(n.Int("color", 0));
    out->Pitch = 0;
    out->FCut = 0.0f;
    out->FRes = 0.0f;
  }

  buf.params = params;
  buf.bytes = bytes;
  buf.count = count;
  return buf;
}

// --- queue + consumed-id ledger ----------------------------------------------

std::vector<int> QueueIds(const std::vector<std::string> &roots) {
  std::vector<int> ids;
  for (const std::string &root : roots) {
    std::vector<int> from_root;
    if (!ReadQueue(root, &from_root)) continue;
    for (int id : from_root) {
      if (!Contains(ids, id)) ids.push_back(id);
    }
  }
  std::sort(ids.begin(), ids.end());
  return ids;
}

std::vector<int> ReadConsumedIds(const std::vector<std::string> &roots) {
  std::vector<int> ids;
  for (const std::string &root : roots) {
    std::string text;
    if (!ReadFile(Join(root, "consumed.json"), &text)) continue;
    minijson::Value doc;
    if (!minijson::Parse(text.c_str(), &doc)) continue;
    const minijson::Value *list = doc.Get("ids");
    if (list == nullptr || list->type != minijson::Type::Array) continue;
    for (const minijson::Value &item : list->array) {
      if (item.type != minijson::Type::Number) continue;
      int id = static_cast<int>(item.number);
      if (!Contains(ids, id)) ids.push_back(id);
    }
  }
  return ids;
}

bool AlreadyConsumed(const std::vector<std::string> &roots, int id) {
  return Contains(ReadConsumedIds(roots), id);
}

int RecordConsumed(const std::vector<std::string> &roots, int id) {
  std::vector<int> ids = ReadConsumedIds(roots);
  if (!Contains(ids, id)) ids.push_back(id);
  if (static_cast<int>(ids.size()) > kMaxConsumedIds) {
    ids.erase(ids.begin(), ids.begin() + (ids.size() - kMaxConsumedIds));
  }

  std::string json = "{\"ids\": [";
  for (size_t i = 0; i < ids.size(); ++i) {
    if (i != 0) json += ", ";
    json += std::to_string(ids[i]);
  }
  json += "]}";

  // Same tombstone the server writes on timeout and the pyscript writes on
  // consume; the ledger, not the tombstone, is what correctness depends on
  // (a root may be readable but not writable). Keyed by id, so a UDP-carried
  // command tombstones only its own mirrored file (usually absent) and can
  // never clobber a different command queued on disk.
  std::string tombstone = "{\"consumed\": true, \"id\": " + std::to_string(id) + "}";

  int accepted = 0;
  for (const std::string &root : roots) {
    if (WriteFile(Join(root, "consumed.json"), json)) ++accepted;
    std::string command = Join(root, ("commands/" + CommandName(id)).c_str());
    std::string existing;
    if (ReadFile(command, &existing)) WriteFile(command, tombstone);
  }
  return accepted;
}

// --- the executor -------------------------------------------------------------

ExecResult ExecuteCommand(TFruityPlugHost *host, std::intptr_t host_tag,
                          const std::vector<std::string> &roots, int ppq_default,
                          const std::string &payload_json, ResultCache *cache,
                          CaptureControl *capture) {
  ExecResult result;
  PollOutcome &outcome = result.outcome;

  minijson::Value command;
  if (!minijson::Parse(payload_json.c_str(), &command) || command.type != minijson::Type::Object) {
    result.ack_json =
        "{\"id\": 0, \"ok\": false, \"err\": \"unparseable command\", \"backend\": \"bridge\"}";
    return result;
  }

  const minijson::Value *op_value = command.Get("op");
  if (op_value == nullptr || op_value->type != minijson::Type::String) {
    result.ack_json = "{\"id\": " + std::to_string(command.Int("id", 0)) +
                      ", \"ok\": false, \"err\": \"command has no op\", \"backend\": \"bridge\"}";
    return result;
  }
  const std::string op = op_value->text;

  // Liveness probe: an ID-LESS ping is stateless - answered end to end
  // (transport -> queue -> tick -> ack) without touching the ledger, the
  // filesystem, or FL, so heartbeats can never evict real ids from the ledger
  // ring. The ack carries our proto so a mismatched client learns it from the
  // pong itself. A ping that CARRIES an id is a queued command like any other
  // and takes the claim path below - the claim is what prunes it from the
  // queue when the server dies before doing so.
  if (op == "ping" && command.Get("id") == nullptr) {
    outcome.had_command = true;
    outcome.op = op;
    outcome.ok = true;
    result.ack_json = "{\"id\": 0, \"ok\": true, \"op\": \"ping\", \"proto\": " +
                      std::to_string(kProtoVersion) + ", \"backend\": \"bridge\"}";
    return result;
  }

  const int id = command.Int("id", 0);
  if (AlreadyConsumed(roots, id)) {
    // A retransmit (or the other carrier / the pyscript got there first).
    // Re-ack the recorded response verbatim when we still have it; otherwise a
    // minimal consumed ack. Never re-execute.
    result.duplicate = true;
    outcome.id = id;
    outcome.op = op;
    const std::string *cached = cache != nullptr ? cache->Find(id) : nullptr;
    result.ack_json = cached != nullptr
                          ? *cached
                          : "{\"id\": " + std::to_string(id) +
                                ", \"ok\": true, \"duplicate\": true, \"backend\": \"bridge\"}";
    return result;
  }

  outcome.had_command = true;
  outcome.id = id;
  outcome.op = op;

  // Claim before executing: a command that crashes FL must not replay on
  // restart and crash it again. Same ordering as the pyscript (_consume_all
  // runs before the proto check and the handler). THE crash-safety invariant -
  // do not reorder.
  RecordConsumed(roots, id);

  std::string summary;
  CaptureReceipt capture_receipt;
  std::string discover_result;  // wp_* ops embed their pass output here
  const int proto = command.Int("proto", 0);
  if (proto != kProtoVersion) {
    outcome.error = "protocol mismatch: command is v" + std::to_string(proto) + ", bridge is v" +
                    std::to_string(kProtoVersion);
  } else if (op == "ping") {
    outcome.ok = true;
    summary = "pong";
  } else if (op == "capture_start") {
    if (capture == nullptr || roots.empty()) {
      outcome.error = "live audio capture is unavailable";
    } else {
      const minijson::Value *args = command.Get("args");
      const double seconds = args != nullptr ? args->Num("seconds", 0.0) : 0.0;
      const int hint = args != nullptr ? args->Int("sample_rate_hint", 0) : 0;
      if (seconds <= 0.0) outcome.error = "capture_start requires positive seconds";
      else {
        capture_receipt = capture->Start(seconds, hint, roots.front(), id);
        outcome.ok = capture_receipt.ok;
        outcome.error = capture_receipt.error;
        summary = outcome.ok ? "capture started" : "capture did not start";
      }
    }
  } else if (op == "capture_stop") {
    if (capture == nullptr) outcome.error = "live audio capture is unavailable";
    else {
      capture_receipt = capture->Stop();
      outcome.ok = capture_receipt.ok;
      outcome.error = capture_receipt.error;
      summary = outcome.ok ? "capture stopped" : "capture did not stop";
    }
  } else if (bridge_discover::IsDiscoverOp(op)) {
    // Discovery rides the ONE executor: claimed-once like any command (the
    // RecordConsumed above already ran, so a discover call that crashes FL
    // never replays), executed in one shot, result carried in the ack. The
    // ops themselves consult the static ledger + guarded buffers + the dynamic
    // quarantine, so they are safe over any range by construction.
    if (host == nullptr) {
      outcome.error = "no plugin host";
    } else {
      const minijson::Value *args = command.Get("args");
      minijson::Value no_args;
      no_args.type = minijson::Type::Object;
      const minijson::Value &checked_args =
          (args != nullptr && args->type == minijson::Type::Object) ? *args : no_args;
      discover_result = bridge_discover::RunOp(host, host_tag, op, checked_args, roots);
      outcome.ok = true;
      summary = "discovery op " + op + " complete";
    }
  } else if (op == "add_notes" || op == "clear_notes") {
    const minijson::Value *args = command.Get("args");
    minijson::Value no_args;
    no_args.type = minijson::Type::Object;
    const minijson::Value &checked_args =
        (args != nullptr && args->type == minijson::Type::Object) ? *args : no_args;
    if (host == nullptr) {
      outcome.error = "no plugin host";
    } else {
      NotesParamsBuffer buf = BuildNotesParams(checked_args, ppq_default, op == "clear_notes");
      if (buf.params == nullptr) {
        outcome.error = buf.error;
      } else {
        Trace("FHD_AddNotesToPR:enter", id, buf.count);
        fl_abi::HostDispatch(host, host_tag, FHD_AddNotesToPR, 0,
                             reinterpret_cast<intptr_t>(buf.params));
        Trace("FHD_AddNotesToPR:return", id, buf.count);
        fl_abi::HostDispatch(host, host_tag, FHD_SetDirty, 0, 0);
        Trace("FHD_SetDirty:return", id, buf.count);
        outcome.notes_written = buf.count;
        // A note write to PatNum >= 1 made that pattern current (SetCurrentPattern
        // side effect); record it so the heartbeat can publish `pat=`.
        outcome.pattern_after = buf.params->PatNum;
        outcome.ok = true;
        summary = "applied " + std::to_string(buf.count) + " note(s) via FHD_AddNotesToPR at ppq " +
                  std::to_string(buf.ppq);
        if (buf.used_default_ppq && op == "add_notes") {
          summary += " (WARNING: command carried no ppq stamp; assumed default " +
                     std::to_string(ppq_default) + " - horizontal placement unverified)";
        }
      }
    }
  } else {
    outcome.error = "unknown op: " + op;
  }

  // The v2 SpoolResponse shape ({id, ok, op, notes_written, err}) plus
  // diagnostic extras (backend, summary) that pydantic ignores on validate.
  // notes_written is this consumer's applied count - an ECHO, not a readback
  // (FHD_AddNotesToPR returns nothing); the session's receipts must not (and
  // do not) trust it.
  std::string json = "{\"id\": " + std::to_string(id);
  json += outcome.ok ? ", \"ok\": true" : ", \"ok\": false";
  if (IsKnownOp(op)) json += ", \"op\": \"" + op + "\"";
  if (outcome.ok) json += ", \"notes_written\": " + std::to_string(outcome.notes_written);
  if (!outcome.ok) json += ", \"err\": \"" + minijson::Escape(outcome.error) + "\"";
  json += ", \"backend\": \"bridge\"";
  // The discovery result is a JSON object built by bridge_discover; embed it
  // verbatim (not escaped) so fl_mcp.discover reads structured data, not a
  // stringified blob. Only wp_* ops set it.
  if (!discover_result.empty()) json += ", \"result\": " + discover_result;
  if (op == "capture_start" || op == "capture_stop") {
    if (!capture_receipt.path.empty()) json += ", \"path\": \"" + minijson::Escape(capture_receipt.path) + "\"";
    json += ", \"frames_written\": " + std::to_string(capture_receipt.frames_written);
    json += ", \"frames_requested\": " + std::to_string(capture_receipt.frames_requested);
    json += ", \"drop_count\": " + std::to_string(capture_receipt.drop_count);
    json += ", \"sample_rate\": " + std::to_string(capture_receipt.sample_rate);
  }
  if (!summary.empty()) json += ", \"summary\": \"" + minijson::Escape(summary) + "\"";
  json += "}";
  result.ack_json = json;
  if (cache != nullptr) cache->Put(id, json);
  // Written for every claimed command regardless of carrier: fl_doctor, the
  // differential tests, and the server's await_response all read one place.
  WriteResponse(roots, id, json);
  return result;
}

// --- the file-spool carrier ---------------------------------------------------

PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default,
                     ResultCache *cache, CaptureControl *capture) {
  // Lowest fresh id first: the server allocates ids in send order, and the
  // pyscript applies in the same order, so both consumers agree on sequencing.
  std::vector<int> pending = QueueIds(roots);
  if (pending.empty()) return PollOutcome();
  std::vector<int> consumed = ReadConsumedIds(roots);

  std::string text;
  bool found = false;
  for (int candidate : pending) {
    if (Contains(consumed, candidate)) continue;
    if (ReadCommand(roots, candidate, &text)) {
      found = true;
      break;
    }
    // Tombstoned everywhere (server-cancelled) or unreadable: skip, never
    // block the ids queued behind it.
  }
  if (!found) return PollOutcome();

  // The one executor; disposal (the per-id tombstone) happens inside via
  // RecordConsumed, and the response lands in responses/<id>.json.
  return ExecuteCommand(host, host_tag, roots, ppq_default, text, cache, capture).outcome;
}

}  // namespace bridge_core
