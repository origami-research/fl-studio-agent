#include "bridge_udp.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>

namespace bridge_udp {

namespace {

// 16 bytes from /dev/urandom, hex-encoded. Empty string on failure - the
// caller fails closed (no transport without a real random token).
std::string RandomTokenHex() {
  unsigned char raw[16];
  FILE *f = fopen("/dev/urandom", "rb");
  if (f == nullptr) return std::string();
  size_t n = fread(raw, 1, sizeof(raw), f);
  fclose(f);
  if (n != sizeof(raw)) return std::string();
  static const char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(sizeof(raw) * 2);
  for (unsigned char byte : raw) {
    out.push_back(kHex[byte >> 4]);
    out.push_back(kHex[byte & 0xf]);
  }
  return out;
}

// Constant-time comparison so the token check leaks nothing through timing
// (academic on localhost, and free to do right).
bool TokenEquals(const char *a, const char *b, std::size_t len) {
  unsigned char acc = 0;
  for (std::size_t i = 0; i < len; ++i) {
    acc = static_cast<unsigned char>(acc | (a[i] ^ b[i]));
  }
  return acc == 0;
}

bool WriteFileRaw(const std::string &path, const std::string &text) {
  // tmp + rename so a concurrent reader never sees partial JSON (same
  // discipline as spool.write_json_atomic on the Python side).
  std::string tmp = path + ".tmp";
  FILE *f = fopen(tmp.c_str(), "wb");
  if (f == nullptr) return false;
  size_t written = fwrite(text.data(), 1, text.size(), f);
  fclose(f);
  if (written != text.size()) {
    unlink(tmp.c_str());
    return false;
  }
  if (rename(tmp.c_str(), path.c_str()) != 0) {
    unlink(tmp.c_str());
    return false;
  }
  return true;
}

}  // namespace

std::string BuildFrame(const std::string &token_hex, const std::string &payload) {
  std::string frame;
  frame.reserve(kHeaderLen + payload.size());
  frame.append(kMagic, kMagicLen);
  frame.append(token_hex);
  frame.push_back(' ');
  frame.append(payload);
  return frame;
}

// Everything the receive thread touches. Co-owned (shared_ptr) by the thread
// and the transport so a detached thread can never dangle: whichever side
// finishes last frees it. The thread closes the socket fd on exit - it is the
// fd's owner - so Stop() never races a close against a concurrent recvfrom.
struct UdpTransport::Shared {
  int fd = -1;
  int port = 0;
  std::string token_hex;
  std::atomic<bool> stop{false};
  bridge_core::CommandQueue queue;
  std::mutex exit_mu;
  std::condition_variable exit_cv;
  bool exited = false;
  std::atomic<std::uint64_t> received{0};
  std::atomic<std::uint64_t> accepted{0};
  std::atomic<std::uint64_t> rejected_frame{0};
  std::atomic<std::uint64_t> rejected_oversize{0};

  explicit Shared(std::size_t queue_capacity) : queue(queue_capacity) {}

  void ReceiveLoop() {
    // 64 KiB covers the UDP maximum (65,507 payload bytes): no datagram can be
    // silently truncated, oversize is judged against our declared ceiling.
    std::vector<char> buffer(65536);
    while (!stop.load(std::memory_order_relaxed)) {
      struct pollfd pfd;
      pfd.fd = fd;
      pfd.events = POLLIN;
      pfd.revents = 0;
      int ready = poll(&pfd, 1, kPollIntervalMs);
      if (stop.load(std::memory_order_relaxed)) break;
      if (ready <= 0) continue;  // timeout or EINTR: re-check stop and wait again
      struct sockaddr_in src;
      socklen_t src_len = sizeof(src);
      ssize_t n = recvfrom(fd, buffer.data(), buffer.size(), 0,
                           reinterpret_cast<struct sockaddr *>(&src), &src_len);
      if (n < 0) continue;
      received.fetch_add(1, std::memory_order_relaxed);
      // Localhost only: the socket is bound to 127.0.0.1 so the kernel already
      // filters, but verify anyway - the check is one compare and the token's
      // whole point is distrust of local traffic.
      if (src.sin_family != AF_INET || src.sin_addr.s_addr != htonl(INADDR_LOOPBACK)) {
        rejected_frame.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      if (static_cast<std::size_t>(n) < kHeaderLen ||
          memcmp(buffer.data(), kMagic, kMagicLen) != 0 ||
          buffer[kHeaderLen - 1] != ' ' ||
          !TokenEquals(buffer.data() + kMagicLen, token_hex.data(), kTokenHexLen)) {
        rejected_frame.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      std::size_t payload_len = static_cast<std::size_t>(n) - kHeaderLen;
      if (payload_len > kMaxDatagramBytes) {
        rejected_oversize.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      bridge_core::InboundDatagram datagram;
      datagram.payload.assign(buffer.data() + kHeaderLen, payload_len);
      datagram.reply_to.assign(reinterpret_cast<const char *>(&src), sizeof(src));
      accepted.fetch_add(1, std::memory_order_relaxed);
      queue.Push(std::move(datagram));
    }
    close(fd);
    {
      std::lock_guard<std::mutex> lock(exit_mu);
      exited = true;
    }
    exit_cv.notify_all();
  }
};

UdpTransport::UdpTransport(std::shared_ptr<Shared> shared) : shared_(std::move(shared)) {}

std::unique_ptr<UdpTransport> UdpTransport::Start(const std::vector<std::string> &roots,
                                                  std::size_t queue_capacity) {
  auto shared = std::make_shared<Shared>(queue_capacity);

  shared->token_hex = RandomTokenHex();
  if (shared->token_hex.size() != kTokenHexLen) return nullptr;

  shared->fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (shared->fd < 0) return nullptr;

  // Make the declared kMaxDatagramBytes ceiling real: platform default UDP
  // buffers (9 KiB send on macOS) are smaller than a full-size datagram.
  // Best-effort - a refused setsockopt just lowers the practical ceiling and
  // oversized commands take the file spool anyway.
  int buf_bytes = 262144;
  setsockopt(shared->fd, SOL_SOCKET, SO_RCVBUF, &buf_bytes, sizeof(buf_bytes));
  setsockopt(shared->fd, SOL_SOCKET, SO_SNDBUF, &buf_bytes, sizeof(buf_bytes));

  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = 0;  // ephemeral: fixed ports collide (docs/22 s3)
  if (bind(shared->fd, reinterpret_cast<struct sockaddr *>(&addr), sizeof(addr)) != 0) {
    close(shared->fd);
    return nullptr;
  }
  struct sockaddr_in bound;
  socklen_t bound_len = sizeof(bound);
  if (getsockname(shared->fd, reinterpret_cast<struct sockaddr *>(&bound), &bound_len) != 0) {
    close(shared->fd);
    return nullptr;
  }
  shared->port = static_cast<int>(ntohs(bound.sin_port));

  // Publish port + token where the server already reads (docs/22 s3). At
  // least one root must accept the file or nobody can ever discover us.
  std::string port_json = "{\"proto\": " + std::to_string(bridge_core::kProtoVersion) +
                          ", \"transport\": 1, \"port\": " + std::to_string(shared->port) +
                          ", \"token\": \"" + shared->token_hex +
                          "\", \"pid\": " + std::to_string(static_cast<long long>(getpid())) + "}";
  std::vector<std::string> published;
  for (const std::string &root : roots) {
    std::string path = root + "/" + kPortFileName;
    if (WriteFileRaw(path, port_json)) published.push_back(path);
  }
  if (published.empty()) {
    close(shared->fd);
    return nullptr;
  }

  std::unique_ptr<UdpTransport> transport(new UdpTransport(shared));
  transport->port_file_paths_ = std::move(published);
  std::thread([shared]() { shared->ReceiveLoop(); }).detach();
  return transport;
}

UdpTransport::~UdpTransport() { Stop(kJoinTimeoutMs); }

void UdpTransport::Stop(int join_timeout_ms) {
  if (stopped_) return;
  stopped_ = true;
  // Unpublish first so no new client traffic targets a dying transport.
  for (const std::string &path : port_file_paths_) unlink(path.c_str());
  shared_->stop.store(true, std::memory_order_relaxed);
  std::unique_lock<std::mutex> lock(shared_->exit_mu);
  stopped_cleanly_ = shared_->exit_cv.wait_for(lock, std::chrono::milliseconds(join_timeout_ms),
                                               [this] { return shared_->exited; });
  // The thread was started detached and co-owns `shared_`, so there is nothing
  // to join: if it has not exited by the deadline we simply walk away and it
  // frees the shared state on its next poll cycle (at most kPollIntervalMs
  // out). FL's GUI thread is never held hostage (docs/22 s2).
}

bool UdpTransport::stopped_cleanly() const { return stopped_cleanly_; }

bridge_core::CommandQueue &UdpTransport::queue() { return shared_->queue; }

bool UdpTransport::SendAck(const std::string &reply_to, const std::string &payload) {
  if (stopped_ || reply_to.size() != sizeof(struct sockaddr_in)) return false;
  std::string frame = BuildFrame(shared_->token_hex, payload);
  struct sockaddr_in dest;
  memcpy(&dest, reply_to.data(), sizeof(dest));
  ssize_t sent = sendto(shared_->fd, frame.data(), frame.size(), MSG_DONTWAIT,
                        reinterpret_cast<struct sockaddr *>(&dest), sizeof(dest));
  return sent == static_cast<ssize_t>(frame.size());
}

int UdpTransport::port() const { return shared_->port; }

const std::string &UdpTransport::token_hex() const { return shared_->token_hex; }

Stats UdpTransport::stats() const {
  Stats stats;
  stats.received = shared_->received.load(std::memory_order_relaxed);
  stats.accepted = shared_->accepted.load(std::memory_order_relaxed);
  stats.rejected_frame = shared_->rejected_frame.load(std::memory_order_relaxed);
  stats.rejected_oversize = shared_->rejected_oversize.load(std::memory_order_relaxed);
  return stats;
}

}  // namespace bridge_udp
