// A test double shaped like FL 2026's real host: an FPC object, not a C++ one.
//
// This is what makes the call contract in fl_host_abi.h provable with no FL in
// the loop. A mock that inherits `TFruityPlugHost` cannot prove anything about
// the contract - it builds a C++ vtable, so slot 0 IS Dispatcher and the
// crashing call looks correct. This mock lays the VMT out the way FL does:
// 200 bytes of header, then Dispatcher.
//
// The header slots are filled with a trap that counts the call and returns 0.
// So a regression to `host->Dispatcher(...)` does not crash the test - it lands
// on the trap, and `header_slot_calls()` says so by name. That is the offline
// reproduction of the live EAccessViolation: same wrong slot, same payload
// independence, but as a failed assertion instead of a dead DAW.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

#include "fl_host_abi.h"

namespace fl_mock {

// Instance layout mirrors an FPC object: the VMT pointer at +0. FL's host keeps
// its own fields after that (HostVersion, Flags, AppHandle, ...); neither the
// Bridge nor the Probe reads one, so this parks a back-pointer there instead.
class FpcHost {
 public:
  FpcHost() {
    for (std::size_t i = 0; i < fl_abi::kFpcVmtHeaderSlots; ++i) {
      vmt_[i] = reinterpret_cast<const void *>(&FpcHost::TrapSlot);
    }
    vmt_[fl_abi::kFpcVmtHeaderSlots] = reinterpret_cast<const void *>(&FpcHost::DispatcherSlot);
    image_.vmt = vmt_;
    image_.owner = this;
  }
  virtual ~FpcHost() = default;

  FpcHost(const FpcHost &) = delete;
  FpcHost &operator=(const FpcHost &) = delete;

  // The pointer to hand to bridge_core, exactly as FL hands one in.
  TFruityPlugHost *as_host() { return reinterpret_cast<TFruityPlugHost *>(&image_); }

  // Calls that arrived through a VMT *header* slot - i.e. through the C++
  // header's vtable indexing. Must always be 0.
  int header_slot_calls() const { return header_slot_calls_; }

 protected:
  // What FL's TFruityPlugHost.Dispatcher would do.
  virtual std::intptr_t Dispatch(std::intptr_t sender, std::intptr_t id, std::intptr_t index,
                                 std::intptr_t value) = 0;

 private:
  struct Image {
    const void *const *vmt;
    FpcHost *owner;
  };

  static FpcHost *OwnerOf(void *self) { return static_cast<Image *>(self)->owner; }

  static std::intptr_t DispatcherSlot(void *self, std::intptr_t sender, std::intptr_t id,
                                      std::intptr_t index, std::intptr_t value) {
    return OwnerOf(self)->Dispatch(sender, id, index, value);
  }

  static std::intptr_t TrapSlot(void *self, std::intptr_t, std::intptr_t, std::intptr_t,
                                std::intptr_t) {
    ++OwnerOf(self)->header_slot_calls_;
    return 0;
  }

  Image image_{};
  const void *vmt_[fl_abi::kFpcVmtHeaderSlots + 1]{};
  int header_slot_calls_ = 0;
};

}  // namespace fl_mock
