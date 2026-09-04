// The ABI gate is a state machine so every unsafe outcome is testable without FL.
#include <cstdio>
#include <string>

#include "bridge_abi.h"
#include "fl_host_abi.h"
#include "mock_host.h"

namespace {
int failures = 0;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL %d: %s\n", __LINE__, #x); ++failures; } } while (0)
}

int main() {
  // The harmless dispatch uses the FPC VMT offset, not the SDK C++ slot.
  bridge_mock::MockHost host;
  host.palette_count = 16;
  bridge_abi::Handshake ok;
  ok.Begin(fl_abi::HostDispatch(host.as_host(), 7, FHD_GetColorRGBValue, 0, -1));
  ok.ObserveDispatcher(FPD_SetSampleRate, 44100);
  ok.ObserveDispatcher(FPD_SetBlockSize, 128);
  ok.ObserveIdle();
  CHECK(ok.status() == bridge_abi::Status::kOk);
  CHECK(ok.allows_host_dispatch());
  CHECK(host.header_slot_calls() == 0);

  bridge_abi::Handshake wrong_answer;
  host.palette_count = 0;
  wrong_answer.Begin(fl_abi::HostDispatch(host.as_host(), 7, FHD_GetColorRGBValue, 0, -1));
  CHECK(wrong_answer.status() == bridge_abi::Status::kMismatch);
  CHECK(std::string(wrong_answer.detail()) == "palette_count");

  bridge_abi::Handshake no_rate;
  no_rate.Begin(16);
  no_rate.ObserveDispatcher(FPD_SetBlockSize, 128);
  no_rate.ObserveIdle();
  no_rate.Evaluate(3.1);
  CHECK(std::string(no_rate.detail()) == "no_set_sample_rate");

  bridge_abi::Handshake no_tick;
  no_tick.Begin(16);
  no_tick.ObserveDispatcher(FPD_SetSampleRate, 44100);
  no_tick.ObserveDispatcher(FPD_SetBlockSize, 128);
  no_tick.Evaluate(3.1);
  CHECK(std::string(no_tick.detail()) == "no_idle_tick");

  return failures == 0 ? 0 : 1;
}
