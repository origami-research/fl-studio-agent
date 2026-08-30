// Localhost UDP transport for the Bridge plugin (design: docs/22).
//
// POSIX sockets only, no dependencies. Binds 127.0.0.1 on an ephemeral port,
// generates a 128-bit token, publishes both as bridge_udp.json in the spool
// roots the server already reads, and receives one command per datagram:
//
//   FLMCP1 <32-hex token> <command JSON, verbatim>
//
// Threading discipline (docs/22 section 1): the receive thread NEVER calls the
// host - it only authenticates frames (memcmp, no JSON parsing) and enqueues
// into the bounded bridge_core::CommandQueue. The tick drains the queue,
// executes through the one shared executor, and acks with a non-blocking
// sendto in the same frame format. Failure containment (section 2): bounded
// queue with drop-oldest, poll(2) with a 200 ms timeout so the thread always
// re-checks its stop flag, and a join-with-deadline-then-detach shutdown whose
// shared-ownership state makes the detach safe rather than a dangling pointer.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "bridge_core.h"

namespace bridge_udp {

// Frame: "FLMCP1 " + 32 hex chars + " " + payload.
constexpr char kMagic[] = "FLMCP1 ";
constexpr std::size_t kMagicLen = 7;
constexpr std::size_t kTokenHexLen = 32;
constexpr std::size_t kHeaderLen = kMagicLen + kTokenHexLen + 1;  // trailing space
// Declared payload ceiling; larger commands take the file spool (docs/22 s4).
constexpr std::size_t kMaxDatagramBytes = 60000;
constexpr char kPortFileName[] = "bridge_udp.json";
// How long DestroyObject waits for the receive thread before detaching. The
// thread re-checks its stop flag at least every kPollIntervalMs, so the join
// virtually always succeeds; the detach path exists so FL's GUI thread can
// never be held hostage by a wedged thread.
constexpr int kPollIntervalMs = 200;
constexpr int kJoinTimeoutMs = 300;

struct Stats {
  std::uint64_t received = 0;        // datagrams read off the socket
  std::uint64_t accepted = 0;        // authenticated and enqueued
  std::uint64_t rejected_frame = 0;  // bad source/magic/token/undersized
  std::uint64_t rejected_oversize = 0;
};

class UdpTransport {
 public:
  // Binds, generates the token (fail closed if /dev/urandom is unavailable),
  // writes the port file into every root that accepts it (at least one must),
  // and starts the receive thread. Returns null on any failure - the caller
  // carries on with the file spool alone.
  static std::unique_ptr<UdpTransport> Start(const std::vector<std::string> &roots,
                                             std::size_t queue_capacity);
  ~UdpTransport();

  UdpTransport(const UdpTransport &) = delete;
  UdpTransport &operator=(const UdpTransport &) = delete;

  // Idempotent. Removes the port file, signals the thread, waits up to
  // `join_timeout_ms` for it to exit, then joins - or detaches, leaving the
  // thread to free the shared state on its next poll cycle (docs/22 s2).
  void Stop(int join_timeout_ms);
  // True if the last Stop() joined; false if it had to detach.
  bool stopped_cleanly() const;

  bridge_core::CommandQueue &queue();
  // Non-blocking ack from the tick thread; a full socket buffer drops the ack
  // (the client retransmits and the ledger dedupes). Returns false on drop.
  bool SendAck(const std::string &reply_to, const std::string &payload);

  int port() const;
  const std::string &token_hex() const;
  Stats stats() const;

 private:
  struct Shared;  // all state the receive thread touches, co-owned by it
  explicit UdpTransport(std::shared_ptr<Shared> shared);
  std::shared_ptr<Shared> shared_;
  std::vector<std::string> port_file_paths_;
  bool stopped_ = false;
  bool stopped_cleanly_ = false;
};

// Exposed for tests: build the exact frame bytes for a payload.
std::string BuildFrame(const std::string &token_hex, const std::string &payload);

}  // namespace bridge_udp
