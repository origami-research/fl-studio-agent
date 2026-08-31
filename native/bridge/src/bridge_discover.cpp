#include "bridge_discover.h"

#include <sys/mman.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include "bridge_core.h"
#include "fl_dispatch_ledger.h"
#include "fl_host_abi.h"
#include "fl_pascal.h"

namespace bridge_discover {

namespace {

constexpr std::size_t kBufBytes = 256;      // guarded decode buffer
constexpr std::size_t kBufHexBytes = 65;    // first 32 bytes -> 64 hex + NUL
constexpr std::size_t kPointerPeekBytes = 64;
constexpr std::size_t kPointerPeekHex = kPointerPeekBytes * 2 + 1;
constexpr int kMaxTimingIters = 2000;       // bound the time spent on FL's main thread
constexpr int kMaxSweepEnd = 255;           // hard ceiling; skips keep it safe
constexpr int kMaxSteps = 64;               // wp_target step cap

constexpr const char *kInflightFile = "wp_inflight.txt";
constexpr const char *kQuarantineFile = "wp_quarantine.txt";

long long MonotonicNanos() {
  timespec ts{};
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<long long>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

int CompareLongLong(const void *a, const void *b) {
  long long x = *static_cast<const long long *>(a);
  long long y = *static_cast<const long long *>(b);
  return (x > y) - (x < y);
}

std::string Join(const std::string &dir, const char *name) { return dir + "/" + name; }

// The durable in-flight checkpoint: fsync'd so it survives a hard FL kill, which
// is the whole point - a crash must leave a trace a re-issued command can read.
void WriteInflightDurable(const std::string &spool_dir, const char *pass, int id) {
  if (spool_dir.empty()) return;
  std::string text = "pass=" + std::string(pass) + "\nid=" + std::to_string(id) + "\n";
  FILE *f = fopen(Join(spool_dir, kInflightFile).c_str(), "wb");
  if (f == nullptr) return;
  fwrite(text.data(), 1, text.size(), f);
  fflush(f);
  fsync(fileno(f));
  fclose(f);
}

// The id a previous command was mid-call on when it never cleared the
// checkpoint (-1 if none / unreadable). That id is presumed to have taken FL
// down and is quarantined by the next op.
int ReadInflightId(const std::string &spool_dir) {
  if (spool_dir.empty()) return -1;
  FILE *f = fopen(Join(spool_dir, kInflightFile).c_str(), "r");
  if (f == nullptr) return -1;
  char line[256];
  int id = -1;
  while (fgets(line, sizeof(line), f) != nullptr) {
    int v;
    if (sscanf(line, "id=%d", &v) == 1) id = v;
  }
  fclose(f);
  return id;
}

// The dynamic quarantine ledger, one "<id> <reason>" per line, in the spool so a
// crasher this FL build reveals is skipped by later commands. The static ledger
// (fl_dispatch) seeds the same skip; both are consulted by one predicate.
std::vector<int> ReadQuarantine(const std::string &spool_dir) {
  std::vector<int> ids;
  if (spool_dir.empty()) return ids;
  FILE *f = fopen(Join(spool_dir, kQuarantineFile).c_str(), "r");
  if (f == nullptr) return ids;
  char line[256];
  while (fgets(line, sizeof(line), f) != nullptr) {
    int id;
    if (sscanf(line, "%d", &id) == 1) ids.push_back(id);
  }
  fclose(f);
  return ids;
}

void AppendQuarantine(const std::string &spool_dir, int id, const char *reason) {
  if (spool_dir.empty()) return;
  FILE *f = fopen(Join(spool_dir, kQuarantineFile).c_str(), "a");
  if (f == nullptr) return;
  fprintf(f, "%d %s\n", id, reason ? reason : "quarantined");
  fclose(f);
}

bool Contains(const std::vector<int> &v, int id) {
  for (int x : v) {
    if (x == id) return true;
  }
  return false;
}

// FL's default note volume, 100/128 - the value the SDK header's own comment
// implies once Vol is read as the Single it is (BRIDGE_FINDINGS.md).
constexpr float kDefaultNoteVol = 100.0f / 128.0f;

// Build and dispatch one FHD_AddNotesToPR. The one legitimate call to id 17 in
// discovery: a real, fully-populated TNotesParams, never the null Value a blind
// sweep would send. Buffer freed on return; a synchronous host call does not
// retain it. Vol written through bridge_core::SetNoteVol - the single writer for
// the Single-typed slot, so the probe's silent-note bug cannot recur here.
std::intptr_t DispatchNotes(TFruityPlugHost *host, std::intptr_t sender, int pattern, int channel,
                            const short *keys, const int *positions, int count, int length_ticks) {
  if (count <= 0) return 0;
  std::size_t bytes = offsetof(TNotesParams, NoteParams) +
                      static_cast<std::size_t>(count) * sizeof(TNoteParams);
  auto *params = static_cast<TNotesParams *>(calloc(1, bytes));
  if (params == nullptr) return 0;
  params->Target = 1;  // piano roll
  params->Flags = 0;
  params->PatNum = pattern;
  params->ChanNum = channel;
  params->Count = count;
  for (int i = 0; i < count; ++i) {
    params->NoteParams[i].Position = positions[i];
    params->NoteParams[i].Length = length_ticks;
    params->NoteParams[i].Note = static_cast<short>(keys[i]);
    bridge_core::SetNoteVol(&params->NoteParams[i], kDefaultNoteVol);
  }
  std::intptr_t ret =
      fl_abi::HostDispatch(host, sender, FHD_AddNotesToPR, 0, reinterpret_cast<std::intptr_t>(params));
  free(params);
  return ret;
}

std::string IdentifyPointer(const fl_pascal::Runtime &pascal, std::intptr_t candidate) {
  if (candidate == 0) return {};
  char name[128];
  if (!pascal.ClassNameOf(reinterpret_cast<const void *>(candidate), name, sizeof(name))) return {};
  return name;
}

}  // namespace

// --- GuardedBuffer (ported from the probe) -----------------------------------

GuardedBuffer::GuardedBuffer(std::size_t size) : size_(size) {
  long page = sysconf(_SC_PAGESIZE);
  if (page <= 0) page = 4096;
  std::size_t pg = static_cast<std::size_t>(page);
  std::size_t usable_pages = (size + pg - 1) / pg;
  if (usable_pages == 0) usable_pages = 1;
  region_bytes_ = (usable_pages + 1) * pg;
  region_ = mmap(nullptr, region_bytes_, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
  if (region_ == MAP_FAILED) {
    region_ = nullptr;
    return;
  }
  auto *base = static_cast<unsigned char *>(region_);
  if (mprotect(base + usable_pages * pg, pg, PROT_NONE) != 0) {
    munmap(region_, region_bytes_);
    region_ = nullptr;
    return;
  }
  data_ = (size == 0) ? base : base + (usable_pages * pg - size);
  Zero();
}

GuardedBuffer::~GuardedBuffer() {
  if (region_ != nullptr) munmap(region_, region_bytes_);
}

void GuardedBuffer::Zero() {
  if (data_ != nullptr) memset(data_, 0, size_);
}

void GuardedBuffer::HexEncode(char *out, std::size_t out_size) const {
  if (out_size == 0) return;
  static const char kHex[] = "0123456789abcdef";
  std::size_t max_bytes = (out_size - 1) / 2;
  std::size_t n = size_ < max_bytes ? size_ : max_bytes;
  const auto *bytes = static_cast<const unsigned char *>(data_);
  for (std::size_t i = 0; i < n; ++i) {
    out[i * 2] = kHex[(bytes[i] >> 4) & 0xF];
    out[i * 2 + 1] = kHex[bytes[i] & 0xF];
  }
  out[n * 2] = '\0';
}

// --- the getter table --------------------------------------------------------

const std::vector<GetterSpec> &Getters() {
  using Call = GetterSpec::Call;
  // GT_Beats=0, GT_Ticks=4 (fp_plugclass.h). Sampling both formats from one id
  // at one instant yields ticks/beat == the project PPQ.
  static const std::vector<GetterSpec> kGetters = {
      {FHD_GetTimeMul, "FHD_GetTimeMul", Call::kIndexOnly, 0,
       "NEGATIVE CONTROL: id 39 is left on FL 2026's default dispatcher branch, "
       "so it must return exactly 0; a nonzero answer refutes the jump-table decode"},
      {FHD_GetMixingTime, "FHD_GetMixingTime[beats]", Call::kBufferInValue, 0,
       "mixer time in beats -> TFPTime.t; pair with [ticks] for PPQ"},
      {FHD_GetMixingTime, "FHD_GetMixingTime[ticks]", Call::kBufferInValue, 4,
       "mixer time in ticks -> TFPTime.t; ticks/beats == PPQ"},
      {FHD_GetPlaybackTime, "FHD_GetPlaybackTime[ticks]", Call::kBufferInValue, 4,
       "song position in ticks -> TFPTime.t; two samples reveal playing state"},
      {FHD_GetPlaybackTime, "FHD_GetPlaybackTime[beats]", Call::kBufferInValue, 0,
       "song position in beats -> TFPTime.t"},
      {FHD_GetSelTime, "FHD_GetSelTime[ticks]", Call::kBufferInValue, 4,
       "selection time in ticks (t,t2); 0 return -> full song length"},
      {FHD_GetContextInfo, "FHD_GetContextInfo[CI_TrackIndex]", Call::kIndexOnly, 1,
       "mixer track index of this plugin; read-only int"},
      {FHD_GetContextInfo, "FHD_GetContextInfo[CI_TrackSelected]", Call::kIndexOnly, 3,
       "is this track selected; read-only int"},
      {FHD_GetContextInfo, "FHD_GetContextInfo[CI_TrackFocused]", Call::kIndexOnly, 4,
       "is this track focused; read-only int"},
      {FHD_GetColorRGBValue, "FHD_GetColorRGBValue[palette0]", Call::kIndexOnly, 0,
       "RGB of color 0 in palette 0; read-only int"},
      {FHD_GetProjectInfo, "FHD_GetProjectInfo[filename]", Call::kReturnsPointer, 4,
       "project naked filename; returns a host PWideChar - log the pointer, never deref"},
  };
  return kGetters;
}

int TargetStepKey(int step) { return 84 + step; }

// --- the passes --------------------------------------------------------------

namespace {

// Quarantine an id a previous command crashed on (its checkpoint was never
// cleared). Returns a JSON note fragment for the result, or "".
std::string RecoverCrashedInflight(const std::string &spool_dir, std::vector<int> *quarantine) {
  int inflight = ReadInflightId(spool_dir);
  if (inflight < 0) return {};
  if (!Contains(*quarantine, inflight)) {
    AppendQuarantine(spool_dir, inflight, "crashed-or-threw-in-flight");
    quarantine->push_back(inflight);
  }
  WriteInflightDurable(spool_dir, "recovered", -1);
  return "\"recovered_crasher\": " + std::to_string(inflight) + ", ";
}

std::string RunGetters(TFruityPlugHost *host, std::intptr_t host_tag, const std::string &spool_dir,
                       const fl_pascal::Runtime &pascal) {
  std::vector<int> quarantine = ReadQuarantine(spool_dir);
  std::string recovered = RecoverCrashedInflight(spool_dir, &quarantine);
  const auto &table = Getters();
  GuardedBuffer buf(kBufBytes);

  std::string results = "[";
  for (std::size_t i = 0; i < table.size(); ++i) {
    const GetterSpec &g = table[i];
    if (i != 0) results += ", ";
    // Getters use the vetted-safe calling convention, so they are exempt from
    // the STATIC ledger but still honour dynamic quarantine.
    if (Contains(quarantine, g.id)) {
      results += "{\"step\": " + std::to_string(i) + ", \"id\": " + std::to_string(g.id) +
                 ", \"getter\": \"" + minijson::Escape(g.name) + "\", \"skipped\": \"quarantined\"}";
      continue;
    }
    if (g.call == GetterSpec::Call::kBufferInValue && !buf.valid()) {
      results += "{\"step\": " + std::to_string(i) + ", \"id\": " + std::to_string(g.id) +
                 ", \"getter\": \"" + minijson::Escape(g.name) + "\", \"skipped\": \"no_guard_buffer\"}";
      continue;
    }
    WriteInflightDurable(spool_dir, "getters", g.id);
    std::intptr_t value = 0;
    char buf_hex[kBufHexBytes] = "";
    if (g.call == GetterSpec::Call::kBufferInValue) {
      buf.Zero();
      value = reinterpret_cast<std::intptr_t>(buf.data());
    }
    std::intptr_t ret = fl_abi::HostDispatch(host, host_tag, g.id, g.index, value);
    if (g.call == GetterSpec::Call::kBufferInValue && buf.valid()) buf.HexEncode(buf_hex, sizeof(buf_hex));
    WriteInflightDurable(spool_dir, "getters", -1);

    const char *kind = g.call == GetterSpec::Call::kBufferInValue    ? "buffer_in_value"
                       : g.call == GetterSpec::Call::kReturnsPointer ? "returns_pointer"
                                                                     : "index_only";
    std::string ret_class = IdentifyPointer(pascal, ret);
    results += "{\"step\": " + std::to_string(i) + ", \"id\": " + std::to_string(g.id) +
               ", \"getter\": \"" + minijson::Escape(g.name) + "\", \"call\": \"" + kind +
               "\", \"index\": " + std::to_string((long long)g.index) +
               ", \"ret\": " + std::to_string((long long)ret) + ", \"ret_class\": \"" +
               minijson::Escape(ret_class) + "\", \"buf_hex\": \"" + buf_hex + "\"}";
  }
  results += "]";
  return "{\"pass\": \"getters\", " + recovered + "\"results\": " + results + "}";
}

std::string RunTiming(TFruityPlugHost *host, std::intptr_t host_tag, const minijson::Value &args,
                      const std::string &spool_dir) {
  int iters = args.Int("iters", 200);
  if (iters < 1) iters = 1;
  if (iters > kMaxTimingIters) iters = kMaxTimingIters;
  std::vector<int> quarantine = ReadQuarantine(spool_dir);
  std::string recovered = RecoverCrashedInflight(spool_dir, &quarantine);
  const auto &table = Getters();
  GuardedBuffer buf(kBufBytes);
  std::vector<long long> samples(static_cast<std::size_t>(iters));

  std::string results = "[";
  long long bundle_ns = 0;
  int bundle_calls = 0;
  bool first = true;
  for (const GetterSpec &g : table) {
    if (Contains(quarantine, g.id) || (g.call == GetterSpec::Call::kBufferInValue && !buf.valid())) {
      continue;  // omit skipped getters from the timing bundle
    }
    if (!first) results += ", ";
    first = false;
    WriteInflightDurable(spool_dir, "timing", g.id);
    std::intptr_t value = 0;
    if (g.call == GetterSpec::Call::kBufferInValue) value = reinterpret_cast<std::intptr_t>(buf.data());
    for (int n = 0; n < iters; ++n) {
      long long t0 = MonotonicNanos();
      std::intptr_t ret = fl_abi::HostDispatch(host, host_tag, g.id, g.index, value);
      long long t1 = MonotonicNanos();
      (void)ret;
      samples[static_cast<std::size_t>(n)] = t1 - t0;
    }
    WriteInflightDurable(spool_dir, "timing", -1);
    qsort(samples.data(), samples.size(), sizeof(long long), CompareLongLong);
    long long total = 0;
    for (long long s : samples) total += s;
    long long median = samples[samples.size() / 2];
    bundle_ns += median;
    bundle_calls++;
    results += "{\"getter\": \"" + minijson::Escape(g.name) + "\", \"id\": " + std::to_string(g.id) +
               ", \"iters\": " + std::to_string(iters) + ", \"min_ns\": " +
               std::to_string(samples.front()) + ", \"median_ns\": " + std::to_string(median) +
               ", \"max_ns\": " + std::to_string(samples.back()) + ", \"mean_ns\": " +
               std::to_string(total / (long long)samples.size()) + "}";
  }
  results += "]";
  return "{\"pass\": \"timing\", " + recovered + "\"getters\": " + results +
         ", \"poll_bundle\": {\"getters\": " + std::to_string(bundle_calls) +
         ", \"median_sum_ns\": " + std::to_string(bundle_ns) + "}}";
}

std::string RunTarget(TFruityPlugHost *host, std::intptr_t host_tag, const minijson::Value &args,
                      const std::string &spool_dir, const fl_pascal::Runtime &pascal) {
  int ppq = args.Int("ppq", 96);
  if (ppq < 1) ppq = 96;
  std::vector<int> quarantine = ReadQuarantine(spool_dir);
  std::string recovered = RecoverCrashedInflight(spool_dir, &quarantine);

  // Optional Sender override (the select-then-write experiment): FL resolves an
  // effect's write target from Sender when Sender IS a channel object. Refused
  // unless it identifies as exactly TPublicChannel - an unverified pointer would
  // be dereferenced by FL's `is` check and take the DAW down.
  std::intptr_t sender = host_tag;
  const char *sender_kind = "plugin_tag";
  const minijson::Value *sender_v = args.Get("sender");
  if (sender_v != nullptr && sender_v->type == minijson::Type::String) {
    std::intptr_t cand = static_cast<std::intptr_t>(strtoull(sender_v->text.c_str(), nullptr, 16));
    if (IdentifyPointer(pascal, cand) == "TPublicChannel") {
      sender = cand;
      sender_kind = "channel_object";
    } else {
      return "{\"pass\": \"target\", " + recovered +
             "\"error\": \"refused_sender: not a TPublicChannel\"}";
    }
  }

  const minijson::Value *steps = args.Get("steps");
  // id 17 is in the static ledger (null-deref hazard), but the target pass is
  // its vetted-safe caller (a real, populated buffer), so it is exempt from the
  // STATIC ledger and gated only by a DYNAMIC quarantine - a live crash inside
  // id 17 on this build.
  bool q17 = Contains(quarantine, FHD_AddNotesToPR);

  std::string results = "[";
  int emitted = 0;
  if (steps != nullptr && steps->type == minijson::Type::Array) {
    for (std::size_t i = 0; i < steps->array.size() && emitted < kMaxSteps; ++i) {
      const minijson::Value &step = steps->array[i];
      if (step.type != minijson::Type::Object) continue;
      int pattern = step.Int("pattern", 0);
      int channel = step.Int("channel", 0);
      short key = static_cast<short>(TargetStepKey(emitted));
      int position = 0;
      std::intptr_t ret = 0;
      if (!q17) {
        WriteInflightDurable(spool_dir, "target", FHD_AddNotesToPR);
        ret = DispatchNotes(host, sender, pattern, channel, &key, &position, 1, ppq);
        WriteInflightDurable(spool_dir, "target", -1);
      }
      if (emitted != 0) results += ", ";
      results += "{\"step\": " + std::to_string(emitted) + ", \"pattern\": " +
                 std::to_string(pattern) + ", \"channel\": " + std::to_string(channel) +
                 ", \"key\": " + std::to_string((int)key) + ", \"ret\": " +
                 std::to_string((long long)ret) + ", \"quarantined\": " + (q17 ? "true" : "false") + "}";
      emitted++;
    }
  }
  results += "]";
  return "{\"pass\": \"target\", " + recovered + "\"sender_kind\": \"" + sender_kind +
         "\", \"ppq\": " + std::to_string(ppq) + ", \"steps\": " + results + "}";
}

std::string RunSweep(TFruityPlugHost *host, std::intptr_t host_tag, const minijson::Value &args,
                     const std::string &spool_dir, const fl_pascal::Runtime &pascal) {
  int start = args.Int("start", 88);
  int end = args.Int("end", fl_dispatch::kHandledMaxId);
  if (start < 0) start = 0;
  if (end > kMaxSweepEnd) end = kMaxSweepEnd;
  std::intptr_t index = static_cast<std::intptr_t>(args.Int("index", 0));
  std::intptr_t value = static_cast<std::intptr_t>(args.Int("value", 0));
  std::vector<int> quarantine = ReadQuarantine(spool_dir);
  std::string recovered = RecoverCrashedInflight(spool_dir, &quarantine);

  // The id set to probe. An explicit `ids` array (map_align's genuinely-new
  // unknowns) probes exactly those; absent it, the contiguous [start,end] range.
  // Either way every id is gated by the same skip predicate below, so an
  // explicit list can never smuggle a documented/unsafe id past the bulkhead.
  std::vector<int> ids;
  const minijson::Value *ids_v = args.Get("ids");
  if (ids_v != nullptr && ids_v->type == minijson::Type::Array) {
    for (const minijson::Value &v : ids_v->array) {
      if (v.type == minijson::Type::Number) ids.push_back(static_cast<int>(v.number));
    }
  } else {
    for (int id = start; id <= end; ++id) ids.push_back(id);
  }

  std::string results = "[";
  bool first = true;
  for (int id : ids) {
    // The blind sweep skips the whole documented range 0-87, every statically
    // unsafe id, and any dynamically quarantined crasher. It only ever
    // dispatches undocumented ids, so it is read-only and safe over any range.
    if (fl_dispatch::IsDocumented(id) || fl_dispatch::IsUnsafe(id) || Contains(quarantine, id)) {
      const fl_dispatch::UnsafeId *u = fl_dispatch::Lookup(id);
      const char *reason = u ? fl_dispatch::HazardName(u->hazard)
                             : (fl_dispatch::IsDocumented(id) ? "documented" : "quarantined");
      if (!first) results += ", ";
      first = false;
      results += "{\"id\": " + std::to_string(id) + ", \"skipped\": \"" + reason + "\"}";
      continue;
    }
    WriteInflightDurable(spool_dir, "sweep", id);
    std::intptr_t ret = fl_abi::HostDispatch(host, host_tag, id, index, value);
    WriteInflightDurable(spool_dir, "sweep", -1);
    std::string ret_class = IdentifyPointer(pascal, ret);
    char ret_hex[kPointerPeekHex] = "";
    if (!ret_class.empty()) {
      pascal.HexAt(reinterpret_cast<const void *>(ret), kPointerPeekBytes, ret_hex, sizeof(ret_hex));
    }
    if (!first) results += ", ";
    first = false;
    results += "{\"id\": " + std::to_string(id) + ", \"ret\": " + std::to_string((long long)ret) +
               ", \"ret_class\": \"" + minijson::Escape(ret_class) + "\", \"ret_hex\": \"" + ret_hex +
               "\", \"beyond_dispatcher\": " + (fl_dispatch::IsBeyondDispatcher(id) ? "true" : "false") +
               "}";
  }
  results += "]";
  return "{\"pass\": \"sweep\", " + recovered + "\"start\": " + std::to_string(start) +
         ", \"end\": " + std::to_string(end) + ", \"results\": " + results + "}";
}

}  // namespace

bool IsDiscoverOp(const std::string &op) {
  return op == "wp_getters" || op == "wp_timing" || op == "wp_target" || op == "wp_sweep";
}

std::string RunOp(TFruityPlugHost *host, std::intptr_t host_tag, const std::string &op,
                  const minijson::Value &args, const std::vector<std::string> &roots) {
  if (host == nullptr) return "{\"error\": \"no plugin host\"}";
  const std::string spool_dir = roots.empty() ? std::string() : roots.front();
  // The FPC class-identification runtime for FL's engine image, derived from the
  // host pointer (an address inside that image). Invalid outside FL (tests),
  // where every IdentifyPointer then returns "" - correct, not a crash.
  fl_pascal::Runtime pascal(host);
  if (op == "wp_getters") return RunGetters(host, host_tag, spool_dir, pascal);
  if (op == "wp_timing") return RunTiming(host, host_tag, args, spool_dir);
  if (op == "wp_target") return RunTarget(host, host_tag, args, spool_dir, pascal);
  if (op == "wp_sweep") return RunSweep(host, host_tag, args, spool_dir, pascal);
  return "{\"error\": \"unknown discover op\"}";
}

}  // namespace bridge_discover
