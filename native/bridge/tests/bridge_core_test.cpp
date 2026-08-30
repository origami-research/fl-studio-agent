// Host-shim test for bridge_core: drives PollOnce with a fake spool command
// against a mock TFruityPlugHost and asserts the exact TNotesParams the Bridge
// would hand FHD_AddNotesToPR, plus the consumed-ledger / result-file / tombstone
// protocol shared with the piano roll .pyscript. Built with ASan+UBSan (see the
// bridge_core_test target in native/CMakeLists.txt).
//
// Hand-rolled asserts, no test framework — same dependency-free convention as
// native/spike1/tests/dispatcher_probe_test.cpp. The CHECK macro, MockHost and
// file helpers are shared with bridge_udp_test via test_support.h.

#include <unistd.h>

#include <cstdint>
#include <cstdlib>
#include <string>
#include <vector>

#include "bridge_core.h"
#include "fp_plugclass.h"
#include "test_support.h"

int main() {
  char home_tmpl[] = "/tmp/bridge_core_test_XXXXXX";
  char *home = mkdtemp(home_tmpl);
  CHECK(home != nullptr);
  if (home == nullptr) return 1;

  std::vector<std::string> roots = bridge_core::SpoolRoots(home);
  CHECK(roots.size() == 2);
  CHECK(Contains(roots[0], "Piano roll scripts/FLMCP"));
  const std::string preferred = roots[0];
  CHECK(MakeDirs(preferred));

  // --- 1. BuildNotesParams unit conversion -----------------------------------
  {
    const char *json =
        "{\"proto\":1,\"id\":7,\"op\":\"add_notes\",\"mode\":\"replace\",\"channel\":2,"
        "\"pattern\":-1,\"notes\":["
        "{\"key\":60,\"start\":0.0,\"length\":1.0,\"velocity\":1.0,\"pan\":0.5,\"color\":3},"
        "{\"key\":72,\"start\":2.5,\"length\":0.5,\"velocity\":0.5,\"pan\":1.0,\"color\":0}"
        "]}";
    minijson::Value cmd;
    CHECK(minijson::Parse(json, &cmd));
    bridge_core::NotesParamsBuffer buf = bridge_core::BuildNotesParams(cmd, bridge_core::kDefaultPPQ);
    CHECK(buf.params != nullptr);
    CHECK(buf.count == 2);
    CHECK(buf.params->Target == 1);                 // piano roll
    CHECK(buf.params->Flags == NPF_EmptyFirst);     // mode=replace
    CHECK(buf.params->PatNum == -1);
    CHECK(buf.params->ChanNum == 2);                // command's channel
    // note 0: start 0 beats -> 0 ticks; length 1 beat -> 96 ticks; vel 1.0 -> 128
    CHECK(buf.params->NoteParams[0].Note == 60);
    CHECK(buf.params->NoteParams[0].Position == 0);
    CHECK(buf.params->NoteParams[0].Length == 96);
    CHECK(buf.params->NoteParams[0].Vol == 128);
    CHECK(buf.params->NoteParams[0].Pan == 0);       // pan 0.5 -> centre 0
    CHECK(buf.params->NoteParams[0].Color == 3);
    // note 1: start 2.5 beats -> 240 ticks; length 0.5 -> 48; vel 0.5 -> 64; pan 1.0 -> +64
    CHECK(buf.params->NoteParams[1].Note == 72);
    CHECK(buf.params->NoteParams[1].Position == 240);
    CHECK(buf.params->NoteParams[1].Length == 48);
    CHECK(buf.params->NoteParams[1].Vol == 64);
    CHECK(buf.params->NoteParams[1].Pan == 64);
  }

  // --- 2. PollOnce end-to-end: fake command -> host call + ledger + result ---
  {
    const char *json =
        "{\"proto\":1,\"id\":42,\"op\":\"add_notes\",\"channel\":5,\"notes\":["
        "{\"key\":64,\"start\":1.0,\"length\":2.0,\"velocity\":0.8,\"pan\":0.5,\"color\":1}"
        "]}";
    WriteTestFile(preferred + "/pending.json", json);

    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(&host, /*host_tag=*/0x1122334455667788LL, roots, bridge_core::kDefaultPPQ);

    CHECK(out.had_command);
    CHECK(out.ok);
    CHECK(out.id == 42);
    CHECK(out.op == "add_notes");
    CHECK(out.notes_written == 1);

    // Host received exactly one chord with the converted note.
    CHECK(host.chords.size() == 1);
    CHECK(host.set_dirty_calls == 1);
    if (!host.chords.empty()) {
      const CapturedChord &c = host.chords[0];
      CHECK(c.target == 1);
      CHECK(c.flags == 0);              // no replace -> merge
      CHECK(c.chan_num == 5);
      CHECK(c.count == 1);
      CHECK(c.notes.size() == 1);
      CHECK(c.notes[0].Note == 64);
      CHECK(c.notes[0].Position == 96);  // 1 beat
      CHECK(c.notes[0].Length == 192);   // 2 beats
    }

    // Ledger records id 42; result.json written; pending tombstoned.
    CHECK(bridge_core::AlreadyConsumed(roots, 42));
    std::string result = ReadTestFile(preferred + "/result.json");
    CHECK(Contains(result, "\"id\": 42"));
    CHECK(Contains(result, "\"ok\": true"));
    CHECK(Contains(result, "\"backend\": \"bridge\""));
    std::string pending_after = ReadTestFile(preferred + "/pending.json");
    CHECK(Contains(pending_after, "\"consumed\": true"));
  }

  // --- 3. Idempotency: the same command is not applied twice ------------------
  {
    // pending.json is a tombstone now; re-poll must be a no-op.
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(&host, 0, roots, bridge_core::kDefaultPPQ);
    CHECK(!out.had_command);
    CHECK(host.chords.empty());

    // Even if a stale live command with the same id reappears, the ledger blocks it.
    WriteTestFile(preferred + "/pending.json",
              "{\"proto\":1,\"id\":42,\"op\":\"add_notes\",\"notes\":[]}");
    bridge_core::PollOutcome out2 =
        bridge_core::PollOnce(&host, 0, roots, bridge_core::kDefaultPPQ);
    CHECK(!out2.had_command);
    CHECK(host.chords.empty());
  }

  // --- 4. clear_notes maps to a replace with zero notes ----------------------
  {
    WriteTestFile(preferred + "/pending.json",
              "{\"proto\":1,\"id\":43,\"op\":\"clear_notes\",\"channel\":1}");
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(&host, 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(out.ok);
    CHECK(host.chords.size() == 1);
    if (!host.chords.empty()) {
      CHECK(host.chords[0].flags == NPF_EmptyFirst);
      CHECK(host.chords[0].count == 0);
      CHECK(host.chords[0].chan_num == 1);
    }
  }

  // --- 5. protocol mismatch is reported, not applied -------------------------
  {
    WriteTestFile(preferred + "/pending.json",
              "{\"proto\":99,\"id\":44,\"op\":\"add_notes\",\"notes\":[{\"key\":60,\"start\":0,\"length\":1}]}");
    MockHost host;
    bridge_core::PollOutcome out =
        bridge_core::PollOnce(&host, 0, roots, bridge_core::kDefaultPPQ);
    CHECK(out.had_command);
    CHECK(!out.ok);
    CHECK(host.chords.empty());
    CHECK(bridge_core::AlreadyConsumed(roots, 44));  // claimed so it can't loop
    std::string result = ReadTestFile(preferred + "/result.json");
    CHECK(Contains(result, "protocol mismatch"));
  }

  if (g_failures == 0) {
    printf("bridge_core_test: all checks passed\n");
    return 0;
  }
  fprintf(stderr, "bridge_core_test: %d failure(s)\n", g_failures);
  return 1;
}
