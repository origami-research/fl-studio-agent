// Load-time ABI evidence for the FL MCP Bridge.
#pragma once

#include <atomic>
#include <cstdint>

namespace bridge_abi {

enum class Status : int { kProbing, kOk, kMismatch };

// FHD_GetColorRGBValue with Value=-1 returns the palette entry count. It is a
// read-only SDK query with a bounded, non-zero answer on a usable FL host.
constexpr std::intptr_t kMinPaletteEntries = 1;
constexpr std::intptr_t kMaxPaletteEntries = 4096;
constexpr double kHandshakeDeadlineSeconds = 3.0;

class Handshake {
 public:
  void Begin(std::intptr_t palette_entries);
  void ObserveDispatcher(std::intptr_t id, std::intptr_t value);
  void ObserveIdle();
  void Evaluate(double elapsed_seconds);

  Status status() const;
  bool allows_host_dispatch() const { return status() == Status::kOk; }
  const char *detail() const;
  const char *heartbeat_abi() const;

 private:
  void Mismatch(int detail);

  std::atomic<Status> status_{Status::kProbing};
  std::atomic<bool> sample_rate_{false};
  std::atomic<bool> block_size_{false};
  std::atomic<bool> idle_{false};
  // 0 is no failure; values map to detail() and remain stable for heartbeats.
  std::atomic<int> detail_{0};
};

}  // namespace bridge_abi
