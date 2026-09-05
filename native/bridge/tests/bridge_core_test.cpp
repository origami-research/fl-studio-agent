// Host-shim test for bridge_core: drives PollOnce over a fake v2 spool
// (commands/queue.json + per-id command files, as mcp-server/src/fl_mcp/spool.py
// writes them) against a mock TFruityPlugHost, and asserts the exact
// TNotesParams the Bridge would hand FHD_AddNotesToPR plus the consumed-ledger
// / response-file / tombstone protocol shared with the piano roll .pyscript.
// Built with ASan+UBSan (see the bridge_core_test target in
// native/CMakeLists.txt).
//
// Hand-rolled asserts, no test framework - dependency-free convention.

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "bridge_core.h"
#include "bridge_capture.h"
#include "bridge_discover.h"
#include "fl_dispatch_ledger.h"
#include "fl_host_abi.h"
#include "fp_plugclass.h"
#include "mock_host.h"

namespace {

using bridge_mock::CapturedChord;
using bridge_mock::MockHost;

int g_failures = 0;

#define CHECK(cond)                                                        \
  do {                                                                     \
    if (!(cond)) {                                                         \
      fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
      g_failures++;                                                        \
    }                                                                      \
  } while (0)

bool MakeDirs(const std::string &path) {
  std::error_code error;
  std::filesystem::create_directories(path, error);
  return !error;
}

std::string TempDir(const char *name) {
  std::error_code error;
  const auto path = std::filesystem::temp_directory_path(error) /
      (std::string(name) + "_" + std::to_string(
          std::chrono::steady_clock::now().time_since_epoch().count()));
  if (error || !std::filesystem::create_directory(path, error)) return {};
  return path.string();
}

void WriteFile(const std::string &path, const std::string &text) {
  std::ofstream f(path, std::ios::trunc);
  f << text;
}

std::string ReadFile(const std::string &path) {
  std::ifstream f(path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

bool Contains(const std::string &haystack, const std::string &needle) {
  return haystack.find(needle) != std::string::npos;
}

std::string ZeroPadded(int id) {
  char name[32];
  snprintf(name, sizeof(name), "%06d.json", id);
  return name;
}

std::string CmdPath(const std::string &root, int id) { return root + "/commands/" + ZeroPadded(id); }

class FakeCapture final : public bridge_core::CaptureControl {
 public:
  bridge_core::CaptureReceipt Start(double seconds, int sample_rate_hint, const std::string &, int) override {
    started_seconds = seconds;
    started_rate = sample_rate_hint;
    return {true, "/tmp/capture.wav", 0, 0, 0, 48000, ""};
  }
  bridge_core::CaptureReceipt Stop() override { return {true, "/tmp/capture.wav", 12, 12, 0, 48000, ""}; }
  double started_seconds = 0;
  int started_rate = 0;
};

// Little-endian writers for the byte-exact TNotesParams expectation. FL 2026
// runs on x86_64 and arm64, both little-endian, so the record RTTI's offsets
// plus these are the whole wire form.
void PutI32(unsigned char *at, int value) { memcpy(at, &value, sizeof(value)); }
void PutI16(unsigned char *at, short value) { memcpy(at, &value, sizeof(value)); }
void PutF32(unsigned char *at, float value) { memcpy(at, &value, sizeof(value)); }

bool AllZero(const void *at, size_t bytes) {
  const unsigned char *p = static_cast<const unsigned char *>(at);
  for (size_t i = 0; i < bytes; ++i) {
    if (p[i] != 0) return false;
  }
  return true;
}

// The HostCallTrace sink is a plain function pointer (it is installed once by
// the plugin shell and never captures), so the recorder it feeds is file-scope.
std::vector<std::string> g_trace;

void RecordTrace(const char *phase, int id, int notes) {
  g_trace.push_back(std::string(phase) + " id=" + std::to_string(id) + " notes=" +
                    std::to_string(notes));
}

std::string RespPath(const std::string &root, int id) { return root + "/responses/" + ZeroPadded(id); }

// The server's queue index ({"proto": 2, "ids": [...]}).
void WriteQueue(const std::string &root, const std::vector<int> &ids) {
  std::string json = "{\"proto\": 2, \"ids\": [";
  for (size_t i = 0; i < ids.size(); ++i) {
    if (i != 0) json += ", ";
    json += std::to_string(ids[i]);
  }
  json += "]}";
  WriteFile(root + "/commands/queue.json", json);
}

}  // namespace

// The block that completes a capture is normally partial (the transport does
// not stop on a block boundary). Its unwanted tail must not count as a ring
// overrun: live run probe 2026-09-04 reported "capture ring overrun" with
// frames_written == frames_requested for exactly this reason.
static void TestCaptureFinalPartialBlockIsNotADrop() {
  bridge_capture::Capture capture;
  capture.SetSampleRate(48000);
  const std::string root = TempDir("bridge_capture_test_root");
  CHECK(!root.empty());
  if (root.empty()) return;
  // 16384 frames minimum ring; request exactly 20000 frames.
  const auto start = capture.Start(20000.0 / 48000.0, 48000, root, 7);
  CHECK(start.ok);
  capture.SetPlaying(true);
  std::vector<float> block(512 * 2, 0.25f);
  std::uint64_t pushed = 0;
  while (pushed < 20000 + 512 * 3) {  // three full blocks beyond completion
    capture.Push(block.data(), 512);
    pushed += 512;
  }
  const auto stop = capture.Stop();
  CHECK(stop.frames_written == 20000);
  CHECK(stop.drop_count == 0);
  CHECK(stop.ok);
}

int main() {
  TestCaptureFinalPartialBlockIsNotADrop();
  const std::string home = TempDir("bridge_core_test");
  CHECK(!home.empty());
  if (home.empty()) return 1;

  // Capture commands use the same claimed-once spool executor and carry the
  // artifact receipt the engine needs, without involving a mixer callback.
  {
    FakeCapture capture;
    bridge_core::ExecResult start = bridge_core::ExecuteCommand(
        nullptr, 0, {}, bridge_core::kDefaultPPQ,
        "{\"proto\":2,\"id\":701,\"op\":\"capture_start\",\"args\":{\"seconds\":1.5,\"sample_rate_hint\":48000}}",
        nullptr, &capture);
    CHECK(!start.outcome.ok);  // roots are mandatory for an artifact path
    bridge_core::ExecResult rooted_start = bridge_core::ExecuteCommand(
        nullptr, 0, {home}, bridge_core::kDefaultPPQ,
        "{\"proto\":2,\"id\":703,\"op\":\"capture_start\",\"args\":{\"seconds\":1.5,\"sample_rate_hint\":48000}}",
        nullptr, &capture);
    CHECK(rooted_start.outcome.ok);
    CHECK(capture.started_seconds == 1.5);
    CHECK(capture.started_rate == 48000);
    bridge_core::ExecResult stop = bridge_core::ExecuteCommand(
        nullptr, 0, {home}, bridge_core::kDefaultPPQ,
        "{\"proto\":2,\"id\":702,\"op\":\"capture_stop\",\"args\":{}}", nullptr, &capture);
    CHECK(stop.outcome.ok);
    CHECK(Contains(stop.ack_json, "\"path\": \"/tmp/capture.wav\""));
    CHECK(Contains(stop.ack_json, "\"frames_written\": 12"));
  }

  // Audio callback contract: the SPSC ring accepts interleaved stereo frames
  // without involving FL, and the writer produces a valid float WAV receipt.
  {
    bridge_capture::Capture capture;
    bridge_core::CaptureReceipt started = capture.Start(2.0 / 48000.0, 48000, home, 700);
    CHECK(started.ok);
    capture.SetPlaying(true);
    float samples[] = {0.25f, -0.25f, 0.5f, -0.5f};
    capture.Push(samples, 2);
    // FL can deliver the next block after the exact target is accepted. It is
    // outside this render by definition, not a ring overrun.
    capture.Push(samples, 2);
    bridge_core::CaptureReceipt stopped = capture.Stop();
    CHECK(stopped.ok);
    CHECK(stopped.frames_written == 2);
    CHECK(stopped.drop_count == 0);
    CHECK(ReadFile(stopped.path).size() == 44 + sizeof(samples) / 2);
  }

  std::vector<std::string> roots = bridge_core::SpoolRoots(home.c_str());
  CHECK(roots.size() == 2);
  CHECK(Contains(roots[0], "Piano roll scripts/FLMCP"));
  std::vector<std::string> isolated = bridge_core::SpoolRoots(home.c_str(), "/tmp/flmcp-session-a");
  CHECK(isolated.size() == 1);
  CHECK(isolated[0] == "/tmp/flmcp-session-a");
  const std::string preferred = roots[0];
  CHECK(MakeDirs(preferred + "/commands"));
  CHECK(MakeDirs(preferred + "/responses"));

  // --- 1. BuildNotesParams: v2 args shape, explicit ppq stamp -----------------
  {
    const char *json =
        "{\"target\":{\"pattern\":4,\"channel\":2},\"mode\":\"replace\",\"ppq\":192,"
        "\"notes\":["
        "{\"key\":60,\"start\":0.0,\"length\":1.0,\"velocity\":1.0,\"pan\":0.5,\"color\":3},"
        "{\"key\":72,\"start\":2.5,\"length\":0.5,\"velocity\":0.5,\"pan\":1.0,\"color\":0}"
        "]}";
    minijson::Value args;
    CHECK(minijson::Parse(json, &args));
    bridge_core::NotesParamsBuffer buf =
        bridge_core::BuildNotesParams(args, bridge_core::kDefaultPPQ, /*empty_first=*/false);
    CHECK(buf.params != nullptr);
    CHECK(buf.count == 2);
    CHECK(buf.ppq == 192);
    CHECK(!buf.used_default_ppq);
    CHECK(buf.params->Target == 1);              // piano roll
    CHECK(buf.params->Flags == NPF_EmptyFirst);  // mode=replace
    CHECK(buf.params->PatNum == 4);
    CHECK(buf.params->ChanNum == 2);
    // note 0: start 0 beats -> 0 ticks; length 1 beat -> 192 ticks; vel 1.0 -> Vol 1.0
    CHECK(buf.params->NoteParams[0].Note == 60);
    CHECK(buf.params->NoteParams[0].Position == 0);
    CHECK(buf.params->NoteParams[0].Length == 192);
    CHECK(bridge_core::NoteVol(buf.params->NoteParams[0]) == 1.0f);
    CHECK(buf.params->NoteParams[0].Pan == 0);  // pan 0.5 -> centre 0
    CHECK(buf.params->NoteParams[0].Color == 3);
    // note 1: start 2.5 beats -> 480 ticks; length 0.5 -> 96; vel 0.5 -> Vol 0.5; pan 1.0 -> +64
    CHECK(buf.params->NoteParams[1].Note == 72);
    CHECK(buf.params->NoteParams[1].Position == 480);
    CHECK(buf.params->NoteParams[1].Length == 96);
    CHECK(bridge_core::NoteVol(buf.params->NoteParams[1]) == 0.5f);
    CHECK(buf.params->NoteParams[1].Pan == 64);
    // One spare zeroed note slot always sits past Count, so an inclusive
    // `for i := 0 to Count` inside FL stays in memory we own.
    CHECK(buf.bytes == 20 + 3 * sizeof(TNoteParams));
    CHECK(AllZero(buf.params->NoteParams + buf.count, sizeof(TNoteParams)));
  }

  // --- 1b. The exact bytes FL 2026 receives ----------------------------------
  // The layout contract is not "whatever the fetched C++ header happens to
  // say": it is FLEngine_x64.dylib's own record RTTI (TNotesParams 52 bytes,
  // note array at 20; TNoteParams 32 bytes, Position/Length/Pan @0/4/8, Vol a
  // SINGLE @12, Note/Color smallints @16/18, Pitch @20, FCut/FRes @24/28).
  // Assert the buffer byte for byte against that, so the wire form is
  // verifiable with no FL Studio in the loop.
  {
    const char *json =
        "{\"target\":{\"pattern\":7,\"channel\":2},\"ppq\":96,"
        "\"notes\":[{\"key\":60,\"start\":1.0,\"length\":2.0,\"velocity\":0.78125,"
        "\"pan\":0.75,\"color\":3}]}";
    minijson::Value args;
    CHECK(minijson::Parse(json, &args));
    bridge_core::NotesParamsBuffer buf =
        bridge_core::BuildNotesParams(args, bridge_core::kDefaultPPQ, /*empty_first=*/false);
    CHECK(buf.params != nullptr);

    unsigned char want[20 + 2 * 32] = {0};
    PutI32(want + 0, 1);    // Target  = piano roll
    PutI32(want + 4, 0);    // Flags   = merge
    PutI32(want + 8, 7);    // PatNum
    PutI32(want + 12, 2);   // ChanNum
    PutI32(want + 16, 1);   // Count
    PutI32(want + 20 + 0, 96);    // Position: 1 beat @ ppq 96
    PutI32(want + 20 + 4, 192);   // Length:   2 beats
    PutI32(want + 20 + 8, 32);    // Pan:      0.75 -> +32, centred at 0
    PutF32(want + 20 + 12, 0.78125f);  // Vol: Single, FL's 100/128 default
    PutI16(want + 20 + 16, 60);   // Note
    PutI16(want + 20 + 18, 3);    // Color
    PutI32(want + 20 + 20, 0);    // Pitch
    PutF32(want + 20 + 24, 0.0f);  // FCut
    PutF32(want + 20 + 28, 0.0f);  // FRes
    // want[52..83] stays zero: the spare slot past Count.
    CHECK(buf.bytes == sizeof(want));
    CHECK(memcmp(buf.params, want, sizeof(want)) == 0);
  }

  // --- 2. BuildNotesParams: null/missing ppq falls back, flagged --------------
  {
    const char *json =
        "{\"target\":{\"pattern\":1,\"channel\":0},\"ppq\":null,"
        "\"notes\":[{\"key\":60,\"start\":1.0,\"length\":1.0}]}";
    minijson::Value args;
    CHECK(minijson::Parse(json, &args));
    bridge_core::NotesParamsBuffer buf =
        bridge_core::BuildNotesParams(args, bridge_core::kDefaultPPQ, false);
    CHECK(buf.used_default_ppq);
    CHECK(buf.ppq == 96);
    CHECK(buf.params->Flags == 0);  // no mode -> merge
    CHECK(buf.params->NoteParams[0].Position == 96);
  }

  // --- 2b. A non-concrete target is refused, never sent to FL as -1 ------------
  // schemas.Target already guarantees pattern >= 1 and channel >= 0; the Bridge
  // holds the same line, because an unresolved -1 inside FL is an access
  // violation and an error response is not.
  {
    const char *bad[] = {
        "{\"ppq\":96,\"notes\":[{\"key\":60}]}",                                  // no target
        "{\"target\":{\"pattern\":-1,\"channel\":0},\"notes\":[{\"key\":60}]}",   // current pattern
        "{\"target\":{\"pattern\":1,\"channel\":-1},\"notes\":[{\"key\":60}]}",   // selected channel
        "{\"target\":{\"pattern\":0,\"channel\":0},\"notes\":[{\"key\":60}]}",    // patterns are 1-based
    };
    for (const char *json : bad) {
      minijson::Value args;
      CHECK(minijson::Parse(json, &args));
      bridge_core::NotesParamsBuffer buf =
          bridge_core::BuildNotesParams(args, bridge_core::kDefaultPPQ, false);
      CHECK(buf.params == nullptr);
      CHECK(buf.error != nullptr);
    }
  }

  // --- 3. PollOnce end-to-end: v2 command -> host call + ledger + response ----
  {
    WriteFile(CmdPath(preferred, 42),
              "{\"proto\":2,\"id\":42,\"op\":\"add_notes\",\"args\":{"
              "\"target\":{\"pattern\":2,\"channel\":5},\"mode\":\"merge\",\"ppq\":96,"
              "\"notes\":[{\"key\":64,\"start\":1.0,\"length\":2.0,\"velocity\":0.8,"
              "\"pan\":0.5,\"color\":1}]}}");
    WriteQueue(preferred, {42});

    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(
        host.as_host(), /*host_tag=*/0x1122334455667788LL, roots, bridge_core::kDefaultPPQ);

    CHECK(out.had_command);
    CHECK(out.ok);
    CHECK(out.id == 42);
    CHECK(out.op == "add_notes");
    CHECK(out.notes_written == 1);

    // Host received exactly one chord with the converted note and the target.
    CHECK(host.chords.size() == 1);
    CHECK(host.set_dirty_calls == 1);
    if (!host.chords.empty()) {
      const CapturedChord &c = host.chords[0];
      CHECK(c.target == 1);
      CHECK(c.flags == 0);  // merge
      CHECK(c.pat_num == 2);
      CHECK(c.chan_num == 5);
      CHECK(c.count == 1);
      CHECK(c.notes.size() == 1);
      CHECK(c.notes[0].Note == 64);
      CHECK(c.notes[0].Position == 96);   // 1 beat
      CHECK(c.notes[0].Length == 192);    // 2 beats
    }

    // Ledger records id 42; SpoolResponse written; command tombstoned; the
    // queue index is untouched (the SERVER prunes it, never a consumer).
    CHECK(bridge_core::AlreadyConsumed(roots, 42));
    std::string resp = ReadFile(RespPath(preferred, 42));
    CHECK(Contains(resp, "\"id\": 42"));
    CHECK(Contains(resp, "\"ok\": true"));
    CHECK(Contains(resp, "\"op\": \"add_notes\""));
    CHECK(Contains(resp, "\"notes_written\": 1"));
    CHECK(Contains(resp, "\"backend\": \"bridge\""));
    CHECK(Contains(ReadFile(CmdPath(preferred, 42)), "\"consumed\": true"));
    CHECK(Contains(ReadFile(preferred + "/commands/queue.json"), "42"));

    // Both host calls arrived through TFruityPlugHost's FIRST DECLARED method
    // (VMT+200), never through a VMT header slot. A regression to the C++
    // header's `host->Dispatcher(...)` resolves to slot 0 - vInstanceSize - and
    // is what took FL down live, on every payload including zero notes.
    CHECK(host.header_slot_calls() == 0);
    // The full argument shape, against Fruity Convolver's - FL 2026's own
    // shipping Fruity effect, which calls its host with x0=Self, x1=Sender
    // (its HostTag, loaded from [plug+8]), x2=ID, x3=Index, x4=Value.
    CHECK(host.calls.size() == 2);  // FHD_AddNotesToPR, then FHD_SetDirty
    if (host.calls.size() == 2) {
      const std::intptr_t kTag = static_cast<std::intptr_t>(0x1122334455667788LL);
      CHECK(host.calls[0].sender == kTag);
      CHECK(host.calls[0].id == FHD_AddNotesToPR);
      CHECK(host.calls[0].index == 0);
      CHECK(host.calls[0].value != 0);  // the TNotesParams pointer
      CHECK(host.calls[1].sender == kTag);
      CHECK(host.calls[1].id == FHD_SetDirty);
      CHECK(host.calls[1].index == 0);
      CHECK(host.calls[1].value == 0);
    }
  }

  // --- 4. Idempotency: the same id is never applied twice ---------------------
  {
    // The command is a tombstone now and the id is in the ledger; re-poll no-ops.
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(!out.had_command);
    CHECK(host.chords.empty());

    // Even if a stale live command with the same id reappears, the ledger blocks it.
    WriteFile(CmdPath(preferred, 42),
              "{\"proto\":2,\"id\":42,\"op\":\"add_notes\",\"args\":{\"notes\":[]}}");
    bridge_core::PollOutcome out2 = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(!out2.had_command);
    CHECK(host.chords.empty());
  }

  // --- 5. One command per poll, lowest id first -------------------------------
  {
    WriteFile(CmdPath(preferred, 51),
              "{\"proto\":2,\"id\":51,\"op\":\"ping\",\"args\":{}}");
    WriteFile(CmdPath(preferred, 50),
              "{\"proto\":2,\"id\":50,\"op\":\"ping\",\"args\":{}}");
    WriteQueue(preferred, {42, 51, 50});  // 42 already consumed

    MockHost host;
    bridge_core::PollOutcome first = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(first.had_command);
    CHECK(first.id == 50);
    CHECK(first.ok);
    bridge_core::PollOutcome second = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(second.had_command);
    CHECK(second.id == 51);
    // ping never touches the host.
    CHECK(host.chords.empty());
    CHECK(host.set_dirty_calls == 0);
    CHECK(Contains(ReadFile(RespPath(preferred, 50)), "\"ok\": true"));
    CHECK(Contains(ReadFile(RespPath(preferred, 50)), "\"op\": \"ping\""));
  }

  // --- 6. clear_notes maps to NPF_EmptyFirst with zero notes, target honored --
  {
    WriteFile(CmdPath(preferred, 60),
              "{\"proto\":2,\"id\":60,\"op\":\"clear_notes\",\"args\":{"
              "\"target\":{\"pattern\":3,\"channel\":1}}}");
    WriteQueue(preferred, {60});
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(out.ok);
    CHECK(host.chords.size() == 1);
    if (!host.chords.empty()) {
      CHECK(host.chords[0].flags == NPF_EmptyFirst);
      CHECK(host.chords[0].count == 0);
      CHECK(host.chords[0].pat_num == 3);
      CHECK(host.chords[0].chan_num == 1);
    }
    CHECK(Contains(ReadFile(RespPath(preferred, 60)), "\"notes_written\": 0"));
  }

  // --- 7. protocol mismatch is claimed and refused, never applied -------------
  {
    WriteFile(CmdPath(preferred, 70),
              "{\"proto\":1,\"id\":70,\"op\":\"add_notes\",\"args\":{"
              "\"notes\":[{\"key\":60,\"start\":0,\"length\":1}]}}");
    WriteQueue(preferred, {70});
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(!out.ok);
    CHECK(host.chords.empty());
    CHECK(bridge_core::AlreadyConsumed(roots, 70));  // claimed so it can't loop
    std::string resp = ReadFile(RespPath(preferred, 70));
    CHECK(Contains(resp, "\"ok\": false"));
    CHECK(Contains(resp, "protocol mismatch"));
  }

  // --- 8. claim-before-execute: a command that fails is still consumed --------
  {
    WriteFile(CmdPath(preferred, 80),
              "{\"proto\":2,\"id\":80,\"op\":\"add_notes\",\"args\":{"
              "\"notes\":[{\"key\":60,\"start\":0,\"length\":1}]}}");
    WriteQueue(preferred, {80});
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(/*host=*/nullptr, 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(!out.ok);
    CHECK(out.error == "no plugin host");
    // The claim landed even though execution failed: no replay after a crash.
    CHECK(bridge_core::AlreadyConsumed(roots, 80));
    CHECK(Contains(ReadFile(RespPath(preferred, 80)), "no plugin host"));
  }

  // --- 9. server-cancelled tombstones are skipped, not applied and not stuck --
  {
    WriteFile(CmdPath(preferred, 90), "{\"consumed\": true, \"id\": 90}");  // cancelled
    WriteFile(CmdPath(preferred, 91),
              "{\"proto\":2,\"id\":91,\"op\":\"ping\",\"args\":{}}");
    WriteQueue(preferred, {90, 91});
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(out.id == 91);
    CHECK(!bridge_core::AlreadyConsumed(roots, 90));  // never claimed, only skipped
  }

  // --- 10. fallback root: commands mirrored only there still apply ------------
  {
    const std::string fallback = roots[1];
    CHECK(MakeDirs(fallback + "/commands"));
    WriteFile(CmdPath(fallback, 100),
              "{\"proto\":2,\"id\":100,\"op\":\"add_notes\",\"args\":{"
              "\"target\":{\"pattern\":1,\"channel\":0},\"ppq\":96,"
              "\"notes\":[{\"key\":62,\"start\":0,\"length\":1}]}}");
    WriteQueue(preferred, {});  // preferred root knows nothing
    WriteQueue(fallback, {100});
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(out.id == 100);
    CHECK(out.ok);
    CHECK(host.chords.size() == 1);
    // The response mirrors into both roots (mkdir on demand for the fallback).
    CHECK(Contains(ReadFile(RespPath(preferred, 100)), "\"ok\": true"));
    CHECK(Contains(ReadFile(RespPath(fallback, 100)), "\"ok\": true"));
  }

  // --- 11. missing ppq stamp: applied with default, WARNING in the summary ----
  {
    WriteFile(CmdPath(preferred, 110),
              "{\"proto\":2,\"id\":110,\"op\":\"add_notes\",\"args\":{"
              "\"target\":{\"pattern\":1,\"channel\":0},"
              "\"notes\":[{\"key\":60,\"start\":1,\"length\":1}]}}");
    WriteQueue(preferred, {110});
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.ok);
    CHECK(host.chords.size() == 1);
    if (!host.chords.empty()) CHECK(host.chords[0].notes[0].Position == 96);
    std::string resp = ReadFile(RespPath(preferred, 110));
    CHECK(Contains(resp, "WARNING"));
    CHECK(Contains(resp, "assumed default 96"));
  }

  // --- 12. host-call breadcrumbs bracket every Dispatcher call ---------------
  // A host call that never returns leaves no crash report, no response file and
  // no stack; the ":enter" record with no matching ":return" is the ONLY
  // evidence a live retest can collect. Assert the enter/return pairs exist and
  // carry the id and note count.
  {
    bridge_core::SetHostCallTrace(RecordTrace);
    g_trace.clear();
    WriteFile(CmdPath(preferred, 120),
              "{\"proto\":2,\"id\":120,\"op\":\"add_notes\",\"args\":{"
              "\"target\":{\"pattern\":1,\"channel\":0},\"ppq\":96,"
              "\"notes\":[{\"key\":60,\"start\":0,\"length\":1},"
              "{\"key\":64,\"start\":1,\"length\":1}]}}");
    WriteQueue(preferred, {120});
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.ok);
    CHECK(g_trace.size() == 3);
    if (g_trace.size() == 3) {
      CHECK(g_trace[0] == "FHD_AddNotesToPR:enter id=120 notes=2");
      CHECK(g_trace[1] == "FHD_AddNotesToPR:return id=120 notes=2");
      CHECK(g_trace[2] == "FHD_SetDirty:return id=120 notes=2");
    }

    // A ping touches no host call, so it must leave no breadcrumb - the file is
    // "the last thing we asked FL to do", not "the last thing that happened".
    g_trace.clear();
    WriteFile(CmdPath(preferred, 121), "{\"proto\":2,\"id\":121,\"op\":\"ping\"}");
    WriteQueue(preferred, {121});
    CHECK(bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ).ok);
    CHECK(g_trace.empty());

    bridge_core::SetHostCallTrace(nullptr);  // no sink installed: silent, never fatal
    g_trace.clear();
    WriteFile(CmdPath(preferred, 122),
              "{\"proto\":2,\"id\":122,\"op\":\"add_notes\",\"args\":{\"ppq\":96,"
              "\"target\":{\"pattern\":1,\"channel\":0},"
              "\"notes\":[{\"key\":60,\"start\":0,\"length\":1}]}}");
    WriteQueue(preferred, {122});
    CHECK(bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ).ok);
    CHECK(g_trace.empty());
  }

  // --- 13. junk queue index is ignored, never fatal ---------------------------
  {
    WriteFile(preferred + "/commands/queue.json", "{not json");
    MockHost host;
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(!out.had_command);
  }

  // --- 14. the FL 2026 host call contract (native/abi/fl_host_abi.h) ---------
  // The 2026-08-29 crash was not the payload: `clear_notes` with ZERO notes
  // died with the same three frames as a three-note chord, because FL never
  // reached the buffer. FL 2026 macOS is Free Pascal, its TFruityPlugHost is an
  // FPC class, and an FPC VMT puts 200 bytes of header in front of the first
  // declared method - so the C++ header's slot 0 is vInstanceSize, not
  // Dispatcher. This section is the offline reproduction: the mock host is
  // FPC-shaped, so a call at the wrong offset is counted rather than fatal.
  {
    CHECK(fl_abi::kFpcVmtHeaderSlots == 25);
    CHECK(fl_abi::kFpcVmtHeaderBytes == 200);
    CHECK(fl_abi::kHostDispatcherOffset == 200);
    // The header field table we step over, as decoded from FL's own VMTs: 25
    // contiguous pointer-sized slots, 0..192.
    for (std::size_t i = 0; i < fl_abi::kFpcVmtHeaderSlots; ++i) {
      CHECK(fl_abi::kFpcVmtHeader[i].offset == i * sizeof(void *));
    }
    CHECK(fl_abi::HostMethodOffset(0) == 200);
    CHECK(fl_abi::HostMethodOffset(1) == 208);

    // The zero-note command - the one that proved the payload was innocent -
    // still reaches FL, and still through the FPC slot.
    WriteFile(CmdPath(preferred, 140),
              "{\"proto\":2,\"id\":140,\"op\":\"clear_notes\",\"args\":{"
              "\"target\":{\"pattern\":1,\"channel\":0}}}");
    WriteQueue(preferred, {140});
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.ok);
    CHECK(host.chords.size() == 1);
    CHECK(host.set_dirty_calls == 1);
    CHECK(host.header_slot_calls() == 0);
    if (!host.chords.empty()) CHECK(host.chords[0].count == 0);
  }

  // --- 15. the static fatal-id ledger (bulkhead invariant #1) -----------------
  // The ledger moved from the deleted probe to native/abi/fl_dispatch_ledger.h so
  // the quarantine survives probe deletion. The live-confirmed fatals stay
  // quarantined by construction; the documented range is skipped whole.
  {
    CHECK(fl_dispatch::IsUnsafe(94));   // crashes FL (EAccessViolation), live-confirmed
    CHECK(fl_dispatch::IsUnsafe(99));   // hangs the tick thread, live-confirmed
    CHECK(fl_dispatch::IsUnsafe(106));  // wedges the tick, live-confirmed (issue #92)
    CHECK(fl_dispatch::IsUnsafe(17));   // FHD_AddNotesToPR: null-deref on a bare call
    CHECK(fl_dispatch::Lookup(94) != nullptr);
    CHECK(fl_dispatch::Lookup(94)->hazard == fl_dispatch::Hazard::kCrashesHost);
    CHECK(fl_dispatch::Lookup(99)->hazard == fl_dispatch::Hazard::kHangsHost);
    CHECK(fl_dispatch::Lookup(106)->hazard == fl_dispatch::Hazard::kHangsHost);
    CHECK(fl_dispatch::IsDocumented(0));
    CHECK(fl_dispatch::IsDocumented(87));
    CHECK(!fl_dispatch::IsDocumented(88));
    // Until L2's generated header lands the FL 2026 constant stands in; when it
    // lands the LedgerCoversGeneratedUnsafeIds static_assert guards agreement.
    CHECK(fl_dispatch::kHandledMaxId == 109);
    CHECK(fl_dispatch::IsBeyondDispatcher(110));
    CHECK(!fl_dispatch::IsBeyondDispatcher(109));
  }

  // The Windows build deliberately compiles a discovery stub. Its pure helpers
  // remain link-compatible, but no wp_* command is accepted or dispatched.
#if defined(_WIN32)
  {
    CHECK(!bridge_discover::IsDiscoverOp("wp_sweep"));
    CHECK(bridge_discover::Getters().empty());
    CHECK(bridge_discover::TargetStepKey(0) == 84);
  }
#else
  // --- 16. wp_sweep rides the executor: claimed once, ledger-skipped ----------
  // The probe's blind sweep, folded into the Bridge as a discovery op. It skips
  // the documented range and every ledger fatal by construction, dispatches only
  // undocumented ids, and rides the same claim-before-execute + response path as
  // a note write.
  {
    WriteFile(CmdPath(preferred, 200),
              "{\"proto\":2,\"id\":200,\"op\":\"wp_sweep\",\"args\":{\"start\":88,\"end\":109}}");
    WriteQueue(preferred, {200});
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(out.ok);
    CHECK(bridge_core::AlreadyConsumed(roots, 200));  // claimed: a crash cannot replay it
    // 88..109 is 22 ids; 94, 99 and 106 are ledger-skipped, so 19 reach the host.
    CHECK(host.calls.size() == 19);
    std::string resp = ReadFile(RespPath(preferred, 200));
    CHECK(Contains(resp, "\"result\":"));
    CHECK(Contains(resp, "\"pass\": \"sweep\""));
    CHECK(Contains(resp, "\"id\": 94, \"skipped\": \"crashes_host\""));
    CHECK(Contains(resp, "\"id\": 99, \"skipped\": \"hangs_host\""));
    CHECK(Contains(resp, "\"id\": 106, \"skipped\": \"hangs_host\""));
    CHECK(Contains(resp, "\"id\": 88, \"ret\": 0"));  // a dispatched undocumented id
  }

  // --- 16b. wp_sweep with an explicit `ids` list probes exactly those ----------
  // map_align emits the genuinely-new unknowns; the sweep consumes that list
  // instead of a blind range. The same skip predicate still gates every id, so
  // a documented or ledger-fatal id in the list is skipped by construction.
  {
    WriteFile(CmdPath(preferred, 204),
              "{\"proto\":2,\"id\":204,\"op\":\"wp_sweep\",\"args\":{\"ids\":[88,90,106,17]}}");
    WriteQueue(preferred, {204});
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.ok);
    // 88 and 90 dispatch; 106 is ledger-skipped; 17 is documented-skipped.
    CHECK(host.calls.size() == 2);
    std::string resp = ReadFile(RespPath(preferred, 204));
    CHECK(Contains(resp, "\"id\": 88, \"ret\": 0"));
    CHECK(Contains(resp, "\"id\": 90, \"ret\": 0"));
    CHECK(Contains(resp, "\"id\": 106, \"skipped\": \"hangs_host\""));
    CHECK(Contains(resp, "\"id\": 17, \"skipped\": \"null_deref\""));
  }

  // --- 17. wp_getters calls the curated safe getters via the FPC slot ---------
  {
    WriteFile(CmdPath(preferred, 201), "{\"proto\":2,\"id\":201,\"op\":\"wp_getters\"}");
    WriteQueue(preferred, {201});
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.ok);
    CHECK(host.calls.size() == bridge_discover::Getters().size());  // every getter dispatched
    CHECK(host.header_slot_calls() == 0);  // through VMT+200, never slot 0
    std::string resp = ReadFile(RespPath(preferred, 201));
    CHECK(Contains(resp, "\"pass\": \"getters\""));
    CHECK(Contains(resp, "FHD_GetMixingTime[ticks]"));
  }

  // --- 18. wp_target plants one note per step with a unique key ---------------
  {
    WriteFile(CmdPath(preferred, 202),
              "{\"proto\":2,\"id\":202,\"op\":\"wp_target\",\"args\":{\"ppq\":96,"
              "\"steps\":[{\"pattern\":2,\"channel\":0},{\"pattern\":3,\"channel\":1}]}}");
    WriteQueue(preferred, {202});
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.ok);
    CHECK(host.chords.size() == 2);
    if (host.chords.size() == 2) {
      CHECK(host.chords[0].pat_num == 2);
      CHECK(host.chords[0].chan_num == 0);
      CHECK(host.chords[0].notes.size() == 1);
      CHECK(host.chords[0].notes[0].Note == bridge_discover::TargetStepKey(0));
      CHECK(host.chords[1].pat_num == 3);
      CHECK(host.chords[1].notes[0].Note == bridge_discover::TargetStepKey(1));
    }
    CHECK(Contains(ReadFile(RespPath(preferred, 202)), "\"pass\": \"target\""));
  }

  // --- 19. a crash a prior command left in flight is quarantined, not re-run --
  // The probe's crash-safety, kept: a durable in-flight checkpoint an earlier
  // command never cleared (FL died mid-call) is read by the next wp op, which
  // quarantines that id and skips it - so a crash STRICTLY advances discovery.
  {
    WriteFile(preferred + "/wp_inflight.txt", "pass=sweep\nid=95\n");
    WriteFile(CmdPath(preferred, 203),
              "{\"proto\":2,\"id\":203,\"op\":\"wp_sweep\",\"args\":{\"start\":88,\"end\":109}}");
    WriteQueue(preferred, {203});
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(host.as_host(), 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.ok);
    CHECK(Contains(ReadFile(preferred + "/wp_quarantine.txt"), "95"));
    std::string resp = ReadFile(RespPath(preferred, 203));
    CHECK(Contains(resp, "\"recovered_crasher\": 95"));
    CHECK(Contains(resp, "\"id\": 95, \"skipped\": \"quarantined\""));
    // 88..109 minus 94, 99, 106 (ledger) and now 95 (quarantined) -> 18 host calls.
    CHECK(host.calls.size() == 18);
  }
#endif

  if (g_failures == 0) {
    printf("bridge_core_test: all checks passed\n");
    return 0;
  }
  fprintf(stderr, "bridge_core_test: %d failure(s)\n", g_failures);
  return 1;
}
