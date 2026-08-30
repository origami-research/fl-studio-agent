// Host-shim test for the UDP transport seam (docs/22): queue discipline,
// the one-executor equivalence across carriers, datagram auth/parse failures,
// idempotent re-acks, a real-socket round trip, and bounded shutdown. Built
// with ASan+UBSan; MockHost and helpers shared with bridge_core_test via
// test_support.h.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "bridge_core.h"
#include "bridge_udp.h"
#include "fp_plugclass.h"
#include "test_support.h"

namespace {

// A minimal UDP client for the tests: what bridge_wire.py does, in C.
class TestClient {
 public:
  explicit TestClient(int port) {
    fd_ = socket(AF_INET, SOCK_DGRAM, 0);
    int buf_bytes = 262144;
    setsockopt(fd_, SOL_SOCKET, SO_SNDBUF, &buf_bytes, sizeof(buf_bytes));
    memset(&dest_, 0, sizeof(dest_));
    dest_.sin_family = AF_INET;
    dest_.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    dest_.sin_port = htons(static_cast<uint16_t>(port));
  }
  ~TestClient() { close(fd_); }

  bool Send(const std::string &frame) {
    ssize_t sent = sendto(fd_, frame.data(), frame.size(), 0,
                          reinterpret_cast<struct sockaddr *>(&dest_), sizeof(dest_));
    return sent == static_cast<ssize_t>(frame.size());
  }

  // Receive one datagram within `timeout_ms`; empty string on timeout.
  std::string Recv(int timeout_ms) {
    struct pollfd pfd;
    pfd.fd = fd_;
    pfd.events = POLLIN;
    pfd.revents = 0;
    if (poll(&pfd, 1, timeout_ms) <= 0) return std::string();
    std::vector<char> buffer(65536);
    ssize_t n = recv(fd_, buffer.data(), buffer.size(), 0);
    if (n <= 0) return std::string();
    return std::string(buffer.data(), static_cast<size_t>(n));
  }

 private:
  int fd_ = -1;
  struct sockaddr_in dest_;
};

// Simulate the plugin tick until an ack arrives at `client` or attempts run
// out: drain the queue, execute through the one executor, ack.
std::string PumpUntilAck(bridge_udp::UdpTransport *transport, MockHost *host,
                         const std::vector<std::string> &roots,
                         bridge_core::ResultCache *cache, TestClient *client) {
  for (int attempt = 0; attempt < 200; ++attempt) {
    for (bridge_core::InboundDatagram &datagram : transport->queue().Drain(4)) {
      bridge_core::ExecResult result = bridge_core::ExecuteCommand(
          host, 0, roots, bridge_core::kDefaultPPQ, datagram.payload, "udp", cache);
      transport->SendAck(datagram.reply_to, result.ack_json);
    }
    std::string ack = client->Recv(10);
    if (!ack.empty()) return ack;
  }
  return std::string();
}

}  // namespace

int main() {
  char home_tmpl[] = "/tmp/bridge_udp_test_XXXXXX";
  char *home = mkdtemp(home_tmpl);
  CHECK(home != nullptr);
  if (home == nullptr) return 1;

  std::vector<std::string> roots = bridge_core::SpoolRoots(home);
  const std::string preferred = roots[0];
  CHECK(MakeDirs(preferred));

  // --- 1. CommandQueue: FIFO, bounded, drop-oldest, bounded drain -------------
  {
    bridge_core::CommandQueue queue(3);
    for (int i = 0; i < 5; ++i) {
      bridge_core::InboundDatagram datagram;
      datagram.payload = std::to_string(i);
      queue.Push(std::move(datagram));
    }
    CHECK(queue.dropped() == 2);  // 0 and 1 fell off the front
    std::vector<bridge_core::InboundDatagram> first = queue.Drain(2);
    CHECK(first.size() == 2);
    CHECK(first[0].payload == "2");  // oldest survivor first (FIFO)
    CHECK(first[1].payload == "3");
    std::vector<bridge_core::InboundDatagram> rest = queue.Drain(10);
    CHECK(rest.size() == 1);
    CHECK(rest[0].payload == "4");
    CHECK(queue.Drain(10).empty());
  }

  // --- 2. ResultCache: find, evict-oldest -------------------------------------
  {
    bridge_core::ResultCache cache(2);
    cache.Put(1, "one");
    cache.Put(2, "two");
    cache.Put(3, "three");
    CHECK(cache.Find(1) == nullptr);  // evicted
    CHECK(cache.Find(2) != nullptr && *cache.Find(2) == "two");
    CHECK(cache.Find(3) != nullptr && *cache.Find(3) == "three");
  }

  // --- 3. Executor: ping answers without touching ledger or filesystem --------
  {
    MockHost host;
    bridge_core::ResultCache cache;
    bridge_core::ExecResult result = bridge_core::ExecuteCommand(
        &host, 0, roots, bridge_core::kDefaultPPQ,
        "{\"proto\":1,\"id\":900,\"op\":\"ping\"}", "udp", &cache);
    CHECK(result.outcome.ok);
    CHECK(Contains(result.ack_json, "\"ok\": true"));
    CHECK(Contains(result.ack_json, "\"op\": \"ping\""));
    CHECK(Contains(result.ack_json, "\"proto\": 1"));
    CHECK(!bridge_core::AlreadyConsumed(roots, 900));           // never claimed
    CHECK(ReadTestFile(preferred + "/result.json").empty());    // never written
    CHECK(host.chords.empty());
  }

  // --- 4. Executor: malformed payloads get explicit error acks ----------------
  {
    MockHost host;
    bridge_core::ExecResult bad_json = bridge_core::ExecuteCommand(
        &host, 0, roots, bridge_core::kDefaultPPQ, "{not json", "udp", nullptr);
    CHECK(!bad_json.outcome.had_command);
    CHECK(Contains(bad_json.ack_json, "unparseable"));
    bridge_core::ExecResult no_op = bridge_core::ExecuteCommand(
        &host, 0, roots, bridge_core::kDefaultPPQ, "{\"id\": 7}", "udp", nullptr);
    CHECK(Contains(no_op.ack_json, "no op"));
    CHECK(host.chords.empty());
  }

  // --- 5. One executor, two carriers: UDP apply == file-spool apply -----------
  {
    const std::string command =
        "{\"proto\":1,\"id\":50,\"op\":\"add_notes\",\"channel\":5,\"notes\":["
        "{\"key\":64,\"start\":1.0,\"length\":2.0,\"velocity\":0.8,\"pan\":0.5,\"color\":1}]}";

    MockHost udp_host;
    bridge_core::ResultCache cache;
    bridge_core::ExecResult via_udp = bridge_core::ExecuteCommand(
        &udp_host, 0, roots, bridge_core::kDefaultPPQ, command, "udp", &cache);
    CHECK(via_udp.outcome.ok);
    CHECK(bridge_core::AlreadyConsumed(roots, 50));
    CHECK(Contains(ReadTestFile(preferred + "/result.json"), "\"id\": 50"));

    // The identical command with a fresh id through the file carrier.
    const std::string command51 =
        "{\"proto\":1,\"id\":51,\"op\":\"add_notes\",\"channel\":5,\"notes\":["
        "{\"key\":64,\"start\":1.0,\"length\":2.0,\"velocity\":0.8,\"pan\":0.5,\"color\":1}]}";
    WriteTestFile(preferred + "/pending.json", command51);
    MockHost file_host;
    bridge_core::PollOutcome via_file =
        bridge_core::PollOnce(&file_host, 0, roots, bridge_core::kDefaultPPQ, &cache);
    CHECK(via_file.ok);

    // Same dispatches: identical TNotesParams either way.
    CHECK(udp_host.chords.size() == 1 && file_host.chords.size() == 1);
    CHECK(udp_host.set_dirty_calls == 1 && file_host.set_dirty_calls == 1);
    if (!udp_host.chords.empty() && !file_host.chords.empty()) {
      const CapturedChord &a = udp_host.chords[0];
      const CapturedChord &b = file_host.chords[0];
      CHECK(a.target == b.target && a.flags == b.flags && a.chan_num == b.chan_num);
      CHECK(a.count == b.count && a.notes.size() == 1 && b.notes.size() == 1);
      CHECK(memcmp(&a.notes[0], &b.notes[0], sizeof(TNoteParams)) == 0);
    }

    // One ledger across carriers: id 50 retried via the FILE spool is refused...
    WriteTestFile(preferred + "/pending.json", command);
    MockHost replay_host;
    bridge_core::PollOutcome replay =
        bridge_core::PollOnce(&replay_host, 0, roots, bridge_core::kDefaultPPQ, &cache);
    CHECK(!replay.had_command);
    CHECK(replay_host.chords.empty());
    CHECK(Contains(ReadTestFile(preferred + "/pending.json"), "\"consumed\": true"));

    // ...and id 51 retried via UDP re-acks the recorded result verbatim,
    // without re-executing.
    MockHost dup_host;
    bridge_core::ExecResult dup = bridge_core::ExecuteCommand(
        &dup_host, 0, roots, bridge_core::kDefaultPPQ, command51, "udp", &cache);
    CHECK(dup.duplicate);
    CHECK(dup_host.chords.empty());
    CHECK(Contains(dup.ack_json, "\"id\": 51"));
    CHECK(Contains(dup.ack_json, "\"ok\": true"));

    // A duplicate whose result predates the cache still gets a consumed ack.
    bridge_core::ResultCache empty_cache;
    bridge_core::ExecResult cold_dup = bridge_core::ExecuteCommand(
        &dup_host, 0, roots, bridge_core::kDefaultPPQ, command51, "udp", &empty_cache);
    CHECK(cold_dup.duplicate);
    CHECK(Contains(cold_dup.ack_json, "\"duplicate\": true"));
  }

  // --- 6. Start refuses roots it cannot publish into --------------------------
  {
    std::vector<std::string> nowhere = {std::string(home) + "/does/not/exist"};
    CHECK(bridge_udp::UdpTransport::Start(nowhere, 8) == nullptr);
  }

  // --- 7. Live transport: port file, auth, round trip, oversize, shutdown -----
  {
    auto transport = bridge_udp::UdpTransport::Start(roots, 8);
    CHECK(transport != nullptr);
    if (transport != nullptr) {
      CHECK(transport->port() > 0);
      CHECK(transport->token_hex().size() == bridge_udp::kTokenHexLen);

      // Port file published where the server reads, with port + token + pid.
      std::string port_file = ReadTestFile(preferred + "/" + bridge_udp::kPortFileName);
      CHECK(Contains(port_file, "\"port\": " + std::to_string(transport->port())));
      CHECK(Contains(port_file, transport->token_hex()));
      CHECK(Contains(port_file, "\"pid\": "));

      MockHost host;
      bridge_core::ResultCache cache;
      TestClient client(transport->port());

      // Bad frames never reach the queue: wrong magic, wrong token.
      CHECK(client.Send("NOTMCP garbage"));
      std::string wrong_token(bridge_udp::kTokenHexLen, 'f');
      CHECK(client.Send(bridge_udp::BuildFrame(wrong_token, "{\"op\":\"ping\",\"id\":1}")));

      // A good ping round-trips through queue -> executor -> ack, and the ack
      // frame carries the bridge's token (the client's proof of origin).
      CHECK(client.Send(bridge_udp::BuildFrame(transport->token_hex(),
                                               "{\"proto\":1,\"id\":901,\"op\":\"ping\"}")));
      std::string ack = PumpUntilAck(transport.get(), &host, roots, &cache, &client);
      CHECK(!ack.empty());
      CHECK(ack.size() > bridge_udp::kHeaderLen);
      CHECK(memcmp(ack.data(), bridge_udp::kMagic, bridge_udp::kMagicLen) == 0);
      CHECK(Contains(ack, transport->token_hex()));
      CHECK(Contains(ack, "\"id\": 901"));
      CHECK(Contains(ack, "\"ok\": true"));

      // An add_notes datagram drives the host through the same executor.
      CHECK(client.Send(bridge_udp::BuildFrame(
          transport->token_hex(),
          "{\"proto\":1,\"id\":902,\"op\":\"add_notes\",\"channel\":3,\"notes\":["
          "{\"key\":60,\"start\":0.0,\"length\":1.0,\"velocity\":1.0,\"pan\":0.5}]}")));
      std::string note_ack = PumpUntilAck(transport.get(), &host, roots, &cache, &client);
      CHECK(Contains(note_ack, "\"id\": 902"));
      CHECK(Contains(note_ack, "\"notes_written\": 1"));
      CHECK(host.chords.size() == 1);
      if (!host.chords.empty()) CHECK(host.chords[0].chan_num == 3);

      // Oversized payload: dropped at the socket, counted, never queued.
      std::string oversize(bridge_udp::kMaxDatagramBytes + 1000, 'x');
      CHECK(client.Send(bridge_udp::BuildFrame(transport->token_hex(), oversize)));

      // Give the receive thread a moment to account for everything, then check
      // the counters: 2 rejected frames, 1 oversize, only the 2 good commands
      // accepted.
      bridge_udp::Stats stats;
      for (int i = 0; i < 200; ++i) {
        stats = transport->stats();
        if (stats.received >= 5) break;
        usleep(10000);
      }
      CHECK(stats.received == 5);
      CHECK(stats.accepted == 2);
      CHECK(stats.rejected_frame == 2);
      CHECK(stats.rejected_oversize == 1);
      CHECK(transport->queue().Drain(10).empty());

      // Shutdown: bounded, clean, unpublished.
      auto t0 = std::chrono::steady_clock::now();
      transport->Stop(bridge_udp::kJoinTimeoutMs);
      auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                         std::chrono::steady_clock::now() - t0)
                         .count();
      CHECK(elapsed <= bridge_udp::kJoinTimeoutMs + 100);
      CHECK(transport->stopped_cleanly());
      CHECK(ReadTestFile(preferred + "/" + bridge_udp::kPortFileName).empty());
    }
  }

  // --- 8. Two transports coexist (ephemeral ports never collide) --------------
  {
    auto first = bridge_udp::UdpTransport::Start(roots, 8);
    auto second = bridge_udp::UdpTransport::Start(roots, 8);
    CHECK(first != nullptr && second != nullptr);
    if (first != nullptr && second != nullptr) CHECK(first->port() != second->port());
  }

  if (g_failures == 0) {
    printf("bridge_udp_test: all checks passed\n");
    return 0;
  }
  fprintf(stderr, "bridge_udp_test: %d failure(s)\n", g_failures);
  return 1;
}
