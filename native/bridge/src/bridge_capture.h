// Mixer-thread-safe live audio capture. Eff_Render only calls Push: no host,
// allocation, lock, condition variable, or filesystem operation is allowed
// on that thread. A single writer thread owns the WAV file.
#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "bridge_core.h"

namespace bridge_capture {

class Capture final : public bridge_core::CaptureControl {
 public:
  Capture() = default;
  ~Capture() override;
  bridge_core::CaptureReceipt Start(double seconds, int sample_rate_hint, const std::string &root,
                                    int command_id) override;
  bridge_core::CaptureReceipt Stop() override;
  void SetSampleRate(int sample_rate);
  void SetPlaying(bool playing);
  void Push(const float *interleaved_stereo, int frames);
  bool active() const { return armed_.load(std::memory_order_acquire); }
  std::uint64_t frames_written() const { return frames_written_.load(); }

 private:
  void Writer();
  void WriteHeader(std::uint64_t frames);
  std::vector<float> ring_;
  std::atomic<std::uint64_t> write_{0}, read_{0}, dropped_{0}, frames_written_{0};
  std::atomic<bool> armed_{false}, recording_{false}, stopping_{false};
  std::atomic<std::uint64_t> frames_requested_{0}, last_block_nanos_{0};
  std::thread writer_;
  std::mutex control_mu_;
  std::string path_;
  std::atomic<int> sample_rate_{44100};
  FILE *file_ = nullptr;
};

}  // namespace bridge_capture
