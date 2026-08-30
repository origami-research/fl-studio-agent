// Mock TFruityPlugHost shared by bridge_core_test and bridge_spool_cli: only
// Dispatcher does anything (recording FHD_AddNotesToPR payloads and
// FHD_SetDirty), everything else is a never-called stub. The captured chords
// are deep copies so assertions survive the caller freeing the real
// (flexible-array) TNotesParams.
#pragma once

#include <cstdint>
#include <vector>

#include "fp_plugclass.h"

namespace bridge_mock {

struct CapturedChord {
  int target = 0;
  int flags = 0;
  int pat_num = 0;
  int chan_num = 0;
  int count = 0;
  std::vector<TNoteParams> notes;
};

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

}  // namespace bridge_mock
