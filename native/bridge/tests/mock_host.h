// Mock TFruityPlugHost shared by bridge_core_test, bridge_udp_test and
// bridge_spool_cli.
//
// It is shaped like FL 2026's real host - an FPC object whose VMT carries a
// 200-byte header in front of Dispatcher (native/abi/fl_host_mock.h). That
// shape is the point: a mock that inherited the C++ `TFruityPlugHost` would put
// Dispatcher at vtable slot 0 and so would agree with the call that takes FL
// down. This one disagrees, and `header_slot_calls()` names the disagreement.
//
// Only Dispatcher does anything - recording FHD_AddNotesToPR payloads and
// counting FHD_SetDirty. The captured chords are deep copies so assertions
// survive the caller freeing the real (flexible-array) TNotesParams.
#pragma once

#include <cstdint>
#include <vector>

#include "fl_host_mock.h"
#include "fp_plugclass.h"

namespace bridge_mock {

// One host->FL dispatcher call, in Convolver's argument order.
struct HostCall {
  std::intptr_t sender = 0;
  std::intptr_t id = 0;
  std::intptr_t index = 0;
  std::intptr_t value = 0;
};

struct CapturedChord {
  int target = 0;
  int flags = 0;
  int pat_num = 0;
  int chan_num = 0;
  int count = 0;
  std::vector<TNoteParams> notes;
};

class MockHost : public fl_mock::FpcHost {
 public:
  std::vector<CapturedChord> chords;
  int set_dirty_calls = 0;
  // Every call verbatim, so a test can assert the full argument shape and not
  // just the payload. The shape is Fruity Convolver's, read off FL 2026's own
  // shipping effect: x0=Self, x1=Sender (the plugin's HostTag, from [plug+8]),
  // x2=ID, x3=Index, x4=Value.
  std::vector<HostCall> calls;

 protected:
  std::intptr_t Dispatch(std::intptr_t sender, std::intptr_t id, std::intptr_t index,
                         std::intptr_t value) override {
    calls.push_back({sender, id, index, value});
    if (id == FHD_AddNotesToPR && value != 0) {
      const TNotesParams *p = reinterpret_cast<const TNotesParams *>(value);
      CapturedChord c;
      c.target = p->Target;
      c.flags = p->Flags;
      c.pat_num = p->PatNum;
      c.chan_num = p->ChanNum;
      c.count = p->Count;
      for (int i = 0; i < p->Count; ++i) c.notes.push_back(p->NoteParams[i]);
      chords.push_back(std::move(c));
    } else if (id == FHD_SetDirty) {
      set_dirty_calls++;
    }
    return 0;
  }
};

}  // namespace bridge_mock
