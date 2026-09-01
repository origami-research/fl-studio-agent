// Windows deliberately does not expose the macOS reverse-engineering passes.
// They rely on a measured Free Pascal/Mach-O ABI; returning an explicit error
// keeps the portable note-writing bridge safe by construction.
#include "bridge_discover.h"

namespace bridge_discover {

bool IsDiscoverOp(const std::string &op) {
  (void)op;
  return false;
}

std::string RunOp(TFruityPlugHost *, std::intptr_t, const std::string &, const minijson::Value &,
                  const std::vector<std::string> &) {
  return "{\"error\": \"discovery passes are not enabled on Windows; no dispatcher ids were called\"}";
}

}  // namespace bridge_discover
