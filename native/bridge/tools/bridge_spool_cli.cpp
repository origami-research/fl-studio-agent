// Differential-test shim: consume a v2 spool exactly as the Bridge plugin
// would - same bridge_core::PollOnce, same mock host as the unit test - and
// print one JSON line per consumed command (the poll outcome plus the exact
// TNotesParams the host received). The Python side
// (mcp-server/tests/test_bridge_differential.py) writes the spool with the
// REAL Spool class and asserts over this output: the seam where the two
// languages drift.
//
// Usage: bridge_spool_cli --home <dir> [--ppq N]
// Drains the spool (polls until no fresh command remains), then exits 0.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "bridge_core.h"
#include "mock_host.h"

namespace {

std::string ChordJson(const bridge_mock::CapturedChord &c) {
  std::string json = "{\"target\": " + std::to_string(c.target);
  json += ", \"flags\": " + std::to_string(c.flags);
  json += ", \"pat_num\": " + std::to_string(c.pat_num);
  json += ", \"chan_num\": " + std::to_string(c.chan_num);
  json += ", \"count\": " + std::to_string(c.count);
  json += ", \"notes\": [";
  for (size_t i = 0; i < c.notes.size(); ++i) {
    const TNoteParams &n = c.notes[i];
    if (i != 0) json += ", ";
    json += "{\"note\": " + std::to_string(n.Note);
    json += ", \"position\": " + std::to_string(n.Position);
    json += ", \"length\": " + std::to_string(n.Length);
    json += ", \"vol\": " + std::to_string(bridge_core::NoteVol(n));
    json += ", \"pan\": " + std::to_string(n.Pan);
    json += ", \"color\": " + std::to_string(n.Color) + "}";
  }
  json += "]}";
  return json;
}

}  // namespace

int main(int argc, char **argv) {
  const char *home = nullptr;
  int ppq = bridge_core::kDefaultPPQ;
  for (int i = 1; i < argc; ++i) {
    if (strcmp(argv[i], "--home") == 0 && i + 1 < argc) {
      home = argv[++i];
    } else if (strcmp(argv[i], "--ppq") == 0 && i + 1 < argc) {
      ppq = atoi(argv[++i]);
    } else {
      fprintf(stderr, "usage: bridge_spool_cli --home <dir> [--ppq N]\n");
      return 2;
    }
  }
  if (home == nullptr) {
    fprintf(stderr, "usage: bridge_spool_cli --home <dir> [--ppq N]\n");
    return 2;
  }

  std::vector<std::string> roots = bridge_core::SpoolRoots(home);
  bridge_mock::MockHost host;
  while (true) {
    const size_t chords_before = host.chords.size();
    bridge_core::PollOutcome out = bridge_core::PollOnce(host.as_host(), 0, roots, ppq);
    if (!out.had_command) break;

    std::string json = "{\"id\": " + std::to_string(out.id);
    json += out.ok ? ", \"ok\": true" : ", \"ok\": false";
    json += ", \"op\": \"" + out.op + "\"";
    json += ", \"notes_written\": " + std::to_string(out.notes_written);
    json += ", \"error\": \"" + minijson::Escape(out.error) + "\"";
    json += ", \"chords\": [";
    for (size_t i = chords_before; i < host.chords.size(); ++i) {
      if (i != chords_before) json += ", ";
      json += ChordJson(host.chords[i]);
    }
    json += "]}";
    printf("%s\n", json.c_str());
  }
  return 0;
}
