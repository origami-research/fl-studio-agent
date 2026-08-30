// Shared support for the bridge's host-shim tests: the CHECK macro, the mock
// TFruityPlugHost, and tiny file helpers. Hand-rolled, no test framework -
// same dependency-free convention as native/spike1's tests. One canonical
// MockHost so bridge_core_test and bridge_udp_test cannot drift.
#pragma once

#include <sys/stat.h>

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include "fp_plugclass.h"

inline int g_failures = 0;

#define CHECK(cond)                                                        \
  do {                                                                     \
    if (!(cond)) {                                                         \
      fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
      g_failures++;                                                        \
    }                                                                      \
  } while (0)

// A deep copy of the note payload FL would receive, so assertions survive the
// caller freeing the real (flexible-array) struct.
struct CapturedChord {
  int target = 0;
  int flags = 0;
  int pat_num = 0;
  int chan_num = 0;
  int count = 0;
  std::vector<TNoteParams> notes;
};

// Mocks TFruityPlugHost. Only Dispatcher is used; it records FHD_AddNotesToPR /
// FHD_SetDirty calls. Every other method is a never-called stub.
class MockHost : public TFruityPlugHost {
 public:
  std::vector<CapturedChord> chords;
  int set_dirty_calls = 0;

  intptr_t _stdcall Dispatcher(TPluginTag /*Sender*/, intptr_t ID, intptr_t /*Index*/,
                               intptr_t Value) override {
    if (ID == FHD_AddNotesToPR && Value != 0) {
      TNotesParams *p = (TNotesParams *)Value;
      CapturedChord c;
      c.target = p->Target;
      c.flags = p->Flags;
      c.pat_num = p->PatNum;
      c.chan_num = p->ChanNum;
      c.count = p->Count;
      for (int i = 0; i < p->Count; ++i) c.notes.push_back(p->NoteParams[i]);
      chords.push_back(std::move(c));
    } else if (ID == FHD_SetDirty) {
      set_dirty_calls++;
    }
    return 0;
  }

  // --- unused stubs ---
  void _stdcall OnParamChanged(TPluginTag, int, int) override {}
  void _stdcall OnHint(TPluginTag, char *) override {}
  void _stdcall ComputeLRVol_Old(float &, float &, int, float) override {}
  void _stdcall Voice_Release(intptr_t) override {}
  void _stdcall Voice_Kill(intptr_t, BOOL) override {}
  int _stdcall Voice_ProcessEvent(intptr_t, intptr_t, intptr_t, intptr_t) override { return 0; }
  void _stdcall LockMix_Old() override {}
  void _stdcall UnlockMix_Old() override {}
  void _stdcall MIDIOut_Delayed(TPluginTag, intptr_t) override {}
  void _stdcall MIDIOut(TPluginTag, intptr_t) override {}
  void _stdcall AddWave_32FM_32FS_Ramp(void *, void *, int, float, float, float &, float &) override {}
  void _stdcall AddWave_32FS_32FS_Ramp(void *, void *, int, float, float, float &, float &) override {}
  bool _stdcall LoadSample(TSampleHandle &, char *, PWaveFormatExtensible, int) override { return false; }
  void *_stdcall GetSampleData(TSampleHandle, int &) override { return nullptr; }
  void _stdcall CloseSample(TSampleHandle) override {}
  int _stdcall GetSongMixingTime() override { return 0; }
  double _stdcall GetSongMixingTime_A() override { return 0; }
  double _stdcall GetSongPlayingTime() override { return 0; }
  void _stdcall OnControllerChanged(TPluginTag, intptr_t, intptr_t) override {}
  void *_stdcall GetSendBuffer(intptr_t) override { return nullptr; }
  void _stdcall PlugMsg_Delayed(TPluginTag, intptr_t) override {}
  void _stdcall PlugMsg_Kill(TPluginTag, intptr_t) override {}
  void _stdcall GetSampleInfo(TSampleHandle, PSampleInfo) override {}
  void _stdcall DistWave_32FM(int, int, void *, int, float, float, float) override {}
  void *_stdcall GetMixBuffer(int) override { return nullptr; }
  void *_stdcall GetInsBuffer(TPluginTag, int) override { return nullptr; }
  BOOL _stdcall PromptEdit(int, int, char *, char *, int &) override { return 0; }
  void _stdcall SuspendOutput_Old() override {}
  void _stdcall ResumeOutput_Old() override {}
  void _stdcall GetSampleRegion(TSampleHandle, int, PSampleRegion) override {}
  void _stdcall ComputeLRVol(float &, float &, float, float) override {}
  void _stdcall LockPlugin(TPluginTag) override {}
  void _stdcall UnlockPlugin(TPluginTag) override {}
  void _stdcall LockMix_Shared_Old() override {}
  void _stdcall UnlockMix_Shared_Old() override {}
  void _stdcall GetInBuffer(TPluginTag, intptr_t, PIOBuffer) override {}
  void _stdcall GetOutBuffer(TPluginTag, intptr_t, PIOBuffer) override {}
  TOutVoiceHandle _stdcall TriggerOutputVoice(TVoiceParams *, intptr_t, intptr_t) override { return 0; }
  void _stdcall OutputVoice_Release(TOutVoiceHandle) override {}
  void _stdcall OutputVoice_Kill(TOutVoiceHandle) override {}
  int _stdcall OutputVoice_ProcessEvent(TOutVoiceHandle, intptr_t, intptr_t, intptr_t) override { return 0; }
  BOOL _stdcall PromptEdit_Ex(int, int, const char *, char *, int &, int &, int &, int, int) override { return 0; }
  void _stdcall SuspendOutput(TPluginTag) override {}
  void _stdcall ResumeOutput(TPluginTag) override {}
};

inline bool MakeDirs(const std::string &path) {
  std::string partial;
  for (size_t i = 0; i <= path.size(); ++i) {
    if (i == path.size() || path[i] == '/') {
      if (!partial.empty() && mkdir(partial.c_str(), 0755) != 0 && errno != EEXIST) return false;
    }
    if (i < path.size()) partial.push_back(path[i]);
  }
  return true;
}

inline void WriteTestFile(const std::string &path, const std::string &text) {
  std::ofstream f(path, std::ios::trunc);
  f << text;
}

inline std::string ReadTestFile(const std::string &path) {
  std::ifstream f(path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

inline bool Contains(const std::string &haystack, const std::string &needle) {
  return haystack.find(needle) != std::string::npos;
}
