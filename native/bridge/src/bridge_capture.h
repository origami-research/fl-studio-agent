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
  bridge_core::CaptureReceipt StreamStart(int port) override;
  bridge_core::CaptureReceipt StreamStop() override;
  bool streaming() const override { return streaming_.load(std::memory_order_acquire); }
  std::uint64_t stream_chunks_sent() const override { return stream_sent_.load(); }
  std::uint64_t stream_chunks_dropped() const override { return stream_dropped_.load(); }
  int stream_sample_rate() const override { return stream_rate_.load(); }
  void SetSampleRate(int sample_rate);
  void SetPlaying(bool playing);
  void Push(const float *interleaved_stereo, int frames);
  bool active() const { return armed_.load(std::memory_order_acquire); }
  std::uint64_t frames_written() const { return frames_written_.load(); }

 private:
  void Writer();
  void WriteHeader(std::uint64_t frames);
  void StreamPush(const float *interleaved_stereo, int frames);
  void StreamWriter();
  std::vector<float> ring_;
  std::atomic<std::uint64_t> write_{0}, read_{0}, dropped_{0}, frames_written_{0};
  std::atomic<bool> armed_{false}, recording_{false}, stopping_{false};
  std::atomic<std::uint64_t> frames_requested_{0}, last_block_nanos_{0};
  std::thread writer_;
  std::mutex control_mu_;
  std::string path_;
  std::atomic<int> sample_rate_{44100};
  FILE *file_ = nullptr;
  static constexpr int kStreamFrames = 1024;
  static constexpr int kStreamSlots = 64;
  std::vector<std::int16_t> stream_ring_;
  std::vector<std::int16_t> stream_partial_;
  std::atomic<std::uint64_t> stream_write_{0}, stream_read_{0}, stream_sequence_{0};
  std::atomic<std::uint64_t> stream_sent_{0}, stream_dropped_{0};
  std::atomic<int> stream_partial_frames_{0}, stream_port_{0}, stream_rate_{0};
  std::atomic<bool> streaming_{false}, stream_stopping_{false};
  std::thread stream_writer_;
};

}  // namespace bridge_capture
