#include "bridge_capture.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <chrono>
#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif
#if defined(_WIN32)
#include <direct.h>
#else
#include <sys/stat.h>
#endif

namespace bridge_capture {
namespace {
constexpr std::uint64_t kMinFrames = 16384;
constexpr std::uint64_t kMaxFrames = 48000 * 600;  // schema caps capture at ten minutes

void Put32(FILE *f, std::uint32_t value) { fwrite(&value, sizeof(value), 1, f); }
void Put16(FILE *f, std::uint16_t value) { fwrite(&value, sizeof(value), 1, f); }
}

Capture::~Capture() { StreamStop(); Stop(); }

std::uint64_t NowNanos() {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
}

void Capture::SetSampleRate(int sample_rate) {
  if (sample_rate >= 8000 && sample_rate <= 192000) sample_rate_.store(sample_rate);
}

void Capture::SetPlaying(bool playing) {
  if (playing && armed_.load(std::memory_order_acquire)) recording_.store(true, std::memory_order_release);
}

bridge_core::CaptureReceipt Capture::Start(double seconds, int sample_rate_hint,
                                           const std::string &root, int command_id) {
  std::lock_guard<std::mutex> lock(control_mu_);
  if (armed_.load()) return {false, "", 0, 0, 0, sample_rate_.load(), "a capture is already armed"};
  if (sample_rate_hint >= 8000 && sample_rate_hint <= 192000) SetSampleRate(sample_rate_hint);
  const int sample_rate = sample_rate_.load();
  const std::uint64_t frames = std::min(kMaxFrames, std::max(kMinFrames,
      static_cast<std::uint64_t>(seconds * sample_rate)));
  ring_.assign(static_cast<size_t>(frames * 2), 0.0f);
  write_ = read_ = dropped_ = frames_written_ = 0;
  const std::string captures = root + "/captures";
#if defined(_WIN32)
  _mkdir(captures.c_str());
#else
  mkdir(captures.c_str(), 0755);
#endif
  path_ = captures + "/capture-" + std::to_string(command_id) + ".wav";
  file_ = fopen(path_.c_str(), "wb+");
  if (file_ == nullptr) {
    path_.clear();
    return {false, "", 0, 0, 0, sample_rate, "could not open capture WAV"};
  }
  WriteHeader(0);
  stopping_.store(false);
  frames_requested_.store(static_cast<std::uint64_t>(seconds * sample_rate));
  armed_.store(true, std::memory_order_release);
  recording_.store(false, std::memory_order_release);
  writer_ = std::thread(&Capture::Writer, this);
  return {true, path_, 0, frames_requested_.load(), 0, sample_rate, ""};
}

bridge_core::CaptureReceipt Capture::Stop() {
  std::lock_guard<std::mutex> lock(control_mu_);
  const bool was_armed = armed_.exchange(false, std::memory_order_acq_rel);
  recording_.store(false, std::memory_order_release);
  stopping_.store(true, std::memory_order_release);
  if (writer_.joinable()) writer_.join();
  const std::uint64_t requested = frames_requested_.load();
  const std::uint64_t written = frames_written_.load();
  const std::uint64_t dropped = dropped_.load();
  const bool stalled = written > 0 && written < requested && NowNanos() - last_block_nanos_.load() > 2000000000ULL;
  bridge_core::CaptureReceipt receipt{was_armed && file_ == nullptr && !path_.empty() && written == requested &&
                                      dropped == 0 && !stalled, path_, written, requested, dropped,
                                      sample_rate_.load(), ""};
  if (file_ != nullptr) {
    receipt.ok = false;
    receipt.error = "capture writer did not close";
  }
  if (!was_armed) receipt.error = "no active capture";
  else if (written == 0) receipt.error = "capture never received a render block after playback started";
  else if (stalled) receipt.error = "capture stalled: no render block for 2 s";
  else if (dropped != 0) receipt.error = "capture ring overrun";
  else if (written != requested) receipt.error = "capture stopped before requested sample count";
  return receipt;
}

bridge_core::CaptureReceipt Capture::StreamStart(int port) {
  std::lock_guard<std::mutex> lock(control_mu_);
  if (streaming_.load()) return {false, "", 0, 0, 0, sample_rate_.load(), "a stream is already active"};
  stream_ring_.assign(static_cast<size_t>(kStreamSlots * kStreamFrames * 2), 0);
  stream_partial_.assign(static_cast<size_t>(kStreamFrames * 2), 0);
  stream_write_ = stream_read_ = stream_sequence_ = stream_sent_ = stream_dropped_ = 0;
  stream_partial_frames_ = 0;
  stream_port_ = port;
  stream_rate_ = sample_rate_.load();
  stream_stopping_.store(false, std::memory_order_release);
  streaming_.store(true, std::memory_order_release);
  stream_writer_ = std::thread(&Capture::StreamWriter, this);
  return {true, "", 0, 0, 0, stream_rate_.load(), ""};
}

bridge_core::CaptureReceipt Capture::StreamStop() {
  std::lock_guard<std::mutex> lock(control_mu_);
  const bool active = streaming_.exchange(false, std::memory_order_acq_rel);
  stream_stopping_.store(true, std::memory_order_release);
  if (stream_writer_.joinable()) stream_writer_.join();
  bridge_core::CaptureReceipt receipt{active, "", stream_sent_.load(), 0, stream_dropped_.load(), stream_rate_.load(), ""};
  if (!active) receipt.error = "no active stream";
  return receipt;
}

void Capture::Push(const float *samples, int frames) {
  StreamPush(samples, frames);
  if (!recording_.load(std::memory_order_acquire) || samples == nullptr || frames <= 0) return;
  const std::uint64_t capacity = ring_.size() / 2;
  std::uint64_t write = write_.load(std::memory_order_relaxed);
  const std::uint64_t read = read_.load(std::memory_order_acquire);
  const std::uint64_t free = capacity - std::min(capacity, write - read);
  const std::uint64_t remaining = frames_requested_.load() - frames_written_.load() - (write - read);
  // The block that completes the capture is usually partial: frames past the
  // requested count are not wanted, so they are never "dropped". Only frames
  // we wanted and had no ring space for count as an overrun.
  const std::uint64_t wanted = std::min(remaining, static_cast<std::uint64_t>(frames));
  const std::uint64_t accepted = std::min(free, wanted);
  for (std::uint64_t i = 0; i < accepted; ++i) {
    const size_t at = static_cast<size_t>(((write + i) % capacity) * 2);
    ring_[at] = samples[i * 2];
    ring_[at + 1] = samples[i * 2 + 1];
  }
  write_.store(write + accepted, std::memory_order_release);
  last_block_nanos_.store(NowNanos(), std::memory_order_relaxed);
  if (accepted == remaining) recording_.store(false, std::memory_order_release);
  dropped_.fetch_add(wanted - accepted, std::memory_order_relaxed);
}

void Capture::StreamPush(const float *samples, int frames) {
  if (!streaming_.load(std::memory_order_acquire) || samples == nullptr || frames <= 0) return;
  int partial = stream_partial_frames_.load(std::memory_order_relaxed);
  for (int source = 0; source < frames; ++source) {
    const size_t at = static_cast<size_t>(partial * 2);
    for (int channel = 0; channel < 2; ++channel) {
      const float value = std::max(-1.0f, std::min(1.0f, samples[source * 2 + channel]));
      stream_partial_[at + channel] = static_cast<std::int16_t>(value * 32767.0f);
    }
    ++partial;
    if (partial != kStreamFrames) continue;
    const std::uint64_t write = stream_write_.load(std::memory_order_relaxed);
    const std::uint64_t read = stream_read_.load(std::memory_order_acquire);
    if (write - read >= kStreamSlots) {
      stream_dropped_.fetch_add(1, std::memory_order_relaxed);
    } else {
      const size_t target = static_cast<size_t>((write % kStreamSlots) * kStreamFrames * 2);
      memcpy(stream_ring_.data() + target, stream_partial_.data(), kStreamFrames * 2 * sizeof(std::int16_t));
      stream_write_.store(write + 1, std::memory_order_release);
    }
    partial = 0;
  }
  stream_partial_frames_.store(partial, std::memory_order_relaxed);
}

void Capture::StreamWriter() {
#if defined(_WIN32)
  WSADATA data;
  if (WSAStartup(MAKEWORD(2, 2), &data) != 0) return;
#endif
  const int fd = static_cast<int>(socket(AF_INET, SOCK_DGRAM, 0));
  if (fd < 0) { stream_dropped_.fetch_add(1); return; }
  sockaddr_in destination{};
  destination.sin_family = AF_INET;
  destination.sin_port = htons(static_cast<unsigned short>(stream_port_.load()));
  destination.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  struct Header { char magic[4]; std::uint32_t sequence; std::uint32_t sample_rate; std::uint16_t channels; std::uint16_t frames; std::uint64_t drops; };
  std::vector<unsigned char> packet(sizeof(Header) + kStreamFrames * 2 * sizeof(std::int16_t));
  for (;;) {
    const std::uint64_t read = stream_read_.load(std::memory_order_relaxed);
    const std::uint64_t write = stream_write_.load(std::memory_order_acquire);
    if (read == write) {
      if (stream_stopping_.load(std::memory_order_acquire)) break;
      std::this_thread::yield();
      continue;
    }
    Header header{{'F', 'L', 'A', 'U'}, static_cast<std::uint32_t>(stream_sequence_.fetch_add(1)),
                  static_cast<std::uint32_t>(stream_rate_.load()), 2, kStreamFrames, stream_dropped_.load()};
    memcpy(packet.data(), &header, sizeof(header));
    const size_t source = static_cast<size_t>((read % kStreamSlots) * kStreamFrames * 2);
    memcpy(packet.data() + sizeof(header), stream_ring_.data() + source, kStreamFrames * 2 * sizeof(std::int16_t));
    const int sent = static_cast<int>(sendto(fd, reinterpret_cast<const char *>(packet.data()), static_cast<int>(packet.size()), 0,
        reinterpret_cast<const sockaddr *>(&destination), sizeof(destination)));
    if (sent == static_cast<int>(packet.size())) stream_sent_.fetch_add(1, std::memory_order_relaxed);
    else stream_dropped_.fetch_add(1, std::memory_order_relaxed);
    stream_read_.store(read + 1, std::memory_order_release);
  }
#if defined(_WIN32)
  closesocket(fd); WSACleanup();
#else
  close(fd);
#endif
}

void Capture::Writer() {
  std::vector<float> block(4096 * 2);
  const std::uint64_t capacity = ring_.size() / 2;
  for (;;) {
    const std::uint64_t read = read_.load(std::memory_order_relaxed);
    const std::uint64_t write = write_.load(std::memory_order_acquire);
    const std::uint64_t available = write - read;
    if (available == 0) {
      if (stopping_.load(std::memory_order_acquire)) break;
      std::this_thread::yield();
      continue;
    }
    const std::uint64_t take = std::min<std::uint64_t>(available, 4096);
    for (std::uint64_t i = 0; i < take; ++i) {
      const size_t at = static_cast<size_t>(((read + i) % capacity) * 2);
      block[i * 2] = ring_[at]; block[i * 2 + 1] = ring_[at + 1];
    }
    for (std::uint64_t i = 0; i < take * 2; ++i) {
      const float value = std::max(-1.0f, std::min(1.0f, block[i]));
      const std::int16_t pcm = static_cast<std::int16_t>(value * 32767.0f);
      fwrite(&pcm, sizeof(pcm), 1, file_);
    }
    frames_written_.fetch_add(take, std::memory_order_relaxed);
    read_.store(read + take, std::memory_order_release);
  }
  WriteHeader(frames_written_.load());
  fclose(file_);
  file_ = nullptr;
}

void Capture::WriteHeader(std::uint64_t frames) {
  const std::uint32_t bytes = static_cast<std::uint32_t>(std::min<std::uint64_t>(frames * 4, 0xffffffffu - 36));
  fseek(file_, 0, SEEK_SET);
  fwrite("RIFF", 1, 4, file_); Put32(file_, 36 + bytes); fwrite("WAVEfmt ", 1, 8, file_);
  const int sample_rate = sample_rate_.load();
  Put32(file_, 16); Put16(file_, 1); Put16(file_, 2); Put32(file_, sample_rate);
  Put32(file_, sample_rate * 4); Put16(file_, 4); Put16(file_, 16);
  fwrite("data", 1, 4, file_); Put32(file_, bytes); fflush(file_);
}
}  // namespace bridge_capture
