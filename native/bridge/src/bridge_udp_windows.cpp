// The Windows bridge uses the durable file spool first. UDP is intentionally
// disabled until it is validated against a live Windows FL process; Start()
// fails closed and the caller continues with the spool carrier.
#include "bridge_udp.h"

#include <exception>

namespace bridge_udp {

std::string BuildFrame(const std::string &token_hex, const std::string &payload) {
  return std::string(kMagic) + token_hex + " " + payload;
}

std::unique_ptr<UdpTransport> UdpTransport::Start(const std::vector<std::string> &, std::size_t) {
  return nullptr;
}

UdpTransport::~UdpTransport() = default;
void UdpTransport::Stop(int) {}
bool UdpTransport::stopped_cleanly() const { return false; }
bridge_core::CommandQueue &UdpTransport::queue() {
  std::terminate();  // Start() always returns null, so this is unreachable.
}
bool UdpTransport::SendAck(const std::string &, const std::string &) { return false; }
int UdpTransport::port() const { return 0; }
const std::string &UdpTransport::token_hex() const {
  static const std::string empty;
  return empty;
}
Stats UdpTransport::stats() const { return {}; }

}  // namespace bridge_udp
