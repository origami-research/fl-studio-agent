#include "bridge_abi.h"

#include "fp_plugclass.h"

namespace bridge_abi {

void Handshake::Mismatch(int detail) {
  Status expected = Status::kProbing;
  if (status_.compare_exchange_strong(expected, Status::kMismatch)) detail_.store(detail);
}

void Handshake::Begin(std::intptr_t palette_entries) {
  if (palette_entries < kMinPaletteEntries || palette_entries > kMaxPaletteEntries) {
    Mismatch(1);
  }
}

void Handshake::ObserveDispatcher(std::intptr_t id, std::intptr_t value) {
  if (status() == Status::kMismatch) return;
  if (id == FPD_SetSampleRate && value > 0) sample_rate_.store(true);
  if (id == FPD_SetBlockSize && value > 0) block_size_.store(true);
}

void Handshake::ObserveIdle() {
  if (status() == Status::kMismatch) return;
  idle_.store(true);
  if (sample_rate_.load() && block_size_.load()) status_.store(Status::kOk);
}

void Handshake::Evaluate(double elapsed_seconds) {
  if (status() != Status::kProbing || elapsed_seconds < kHandshakeDeadlineSeconds) return;
  if (!sample_rate_.load()) return Mismatch(2);
  if (!block_size_.load()) return Mismatch(3);
  if (!idle_.load()) return Mismatch(4);
}

Status Handshake::status() const { return status_.load(); }

const char *Handshake::detail() const {
  switch (detail_.load()) {
    case 1: return "palette_count";
    case 2: return "no_set_sample_rate";
    case 3: return "no_set_block_size";
    case 4: return "no_idle_tick";
    default: return "probing";
  }
}

const char *Handshake::heartbeat_abi() const {
  switch (status()) {
    case Status::kOk: return "ok";
    case Status::kMismatch: return "mismatch";
    case Status::kProbing: return "probing";
  }
  return "mismatch";
}

}  // namespace bridge_abi
