#include "fl_pascal.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstring>

#if defined(__APPLE__)
#include <mach-o/loader.h>
#endif

namespace fl_pascal {

namespace {

bool Printable(unsigned char c) { return c >= 0x20 && c < 0x7f; }

// Size of the mapped image starting at `base`, by walking its Mach-O load
// commands with fault-free reads only. Returns 0 if `base` is not a readable
// Mach-O header, which makes every later InImage() test fail closed.
std::size_t ImageSize(const SafeReader &reader, const void *base) {
#if defined(__APPLE__)
  mach_header_64 header{};
  if (!reader.Read(base, &header, sizeof(header))) return 0;
  if (header.magic != MH_MAGIC_64) return 0;
  const auto *cursor = static_cast<const unsigned char *>(base) + sizeof(header);
  std::size_t span = sizeof(header);
  for (uint32_t i = 0; i < header.ncmds && i < 4096; ++i) {
    load_command lc{};
    if (!reader.Read(cursor, &lc, sizeof(lc))) return 0;
    if (lc.cmdsize < sizeof(lc)) return 0;
    if (lc.cmd == LC_SEGMENT_64) {
      segment_command_64 seg{};
      if (!reader.Read(cursor, &seg, sizeof(seg))) return 0;
      std::size_t end = static_cast<std::size_t>(seg.vmaddr) + static_cast<std::size_t>(seg.vmsize);
      // Segment vmaddrs are file-relative for a dylib whose first segment is at
      // vmaddr 0 (FLEngine's shape), so the span is simply the highest end.
      if (end > span) span = end;
    }
    cursor += lc.cmdsize;
  }
  return span;
#else
  (void)reader;
  (void)base;
  return 0;
#endif
}

}  // namespace

// --- SafeReader ------------------------------------------------------------

SafeReader::SafeReader() {
  if (pipe(fds_) != 0) {
    fds_[0] = fds_[1] = -1;
    return;
  }
  // Non-blocking on both ends: a full pipe must fail the read, never park FL's
  // main thread. Every Read() drains what it wrote, so this is belt-and-braces.
  fcntl(fds_[0], F_SETFL, O_NONBLOCK);
  fcntl(fds_[1], F_SETFL, O_NONBLOCK);
}

SafeReader::~SafeReader() {
  if (fds_[0] >= 0) close(fds_[0]);
  if (fds_[1] >= 0) close(fds_[1]);
}

bool SafeReader::Read(const void *src, void *dst, std::size_t n) const {
  if (src == nullptr || dst == nullptr || n == 0 || n > kMaxRead) return false;
  if (fds_[0] < 0) return false;
  // The kernel copies from `src`; an unreadable page yields EFAULT rather than
  // a fault in this process. Nothing here ever dereferences `src`.
  ssize_t wrote = write(fds_[1], src, n);
  if (wrote != static_cast<ssize_t>(n)) {
    // Partial writes cannot happen for n <= kMaxRead on an empty pipe, but if
    // one ever did it would leave stale bytes behind; drain whatever landed.
    if (wrote > 0) {
      unsigned char sink[kMaxRead];
      ssize_t drained = 0;
      while (drained < wrote) {
        ssize_t got = read(fds_[0], sink, sizeof(sink));
        if (got <= 0) break;
        drained += got;
      }
    }
    return false;
  }
  std::size_t got = 0;
  auto *out = static_cast<unsigned char *>(dst);
  while (got < n) {
    ssize_t r = read(fds_[0], out + got, n - got);
    if (r <= 0) return false;
    got += static_cast<std::size_t>(r);
  }
  return true;
}

// --- Runtime ---------------------------------------------------------------

Runtime::Runtime(const void *addr_in_image) {
  if (addr_in_image == nullptr) return;
  Dl_info info{};
  if (dladdr(addr_in_image, &info) == 0 || info.dli_fbase == nullptr) return;
  base_ = info.dli_fbase;
  if (info.dli_fname != nullptr) snprintf(path_, sizeof(path_), "%s", info.dli_fname);
  size_ = ImageSize(reader_, base_);
  if (size_ == 0) base_ = nullptr;  // fail closed: an unwalkable image identifies nothing
}

Runtime::Runtime(const void *image_base, std::size_t image_size)
    : base_(image_base), size_(image_size) {}

bool Runtime::InImage(const void *addr) const {
  if (!valid() || addr == nullptr) return false;
  auto a = reinterpret_cast<uintptr_t>(addr);
  auto b = reinterpret_cast<uintptr_t>(base_);
  return a >= b && a < b + size_;
}

bool Runtime::ClassNameOf(const void *obj, char *out, std::size_t out_size) const {
  if (out == nullptr || out_size == 0) return false;
  out[0] = '\0';
  if (!valid() || obj == nullptr) return false;
  // An FPC object pointer is word-aligned; anything else is not one.
  if (reinterpret_cast<uintptr_t>(obj) % sizeof(void *) != 0) return false;

  const void *vmt = nullptr;
  if (!reader_.Read(obj, &vmt, sizeof(vmt))) return false;
  if (!InImage(vmt)) return false;

  uint64_t instance_size = 0;
  if (!reader_.Read(static_cast<const unsigned char *>(vmt) + kVmtInstanceSize, &instance_size,
                    sizeof(instance_size))) {
    return false;
  }
  if (instance_size == 0 || instance_size > kMaxPlausibleInstanceSize) return false;

  const void *name_ptr = nullptr;
  if (!reader_.Read(static_cast<const unsigned char *>(vmt) + kVmtClassName, &name_ptr,
                    sizeof(name_ptr))) {
    return false;
  }
  if (!InImage(name_ptr)) return false;

  unsigned char raw[1 + kMaxShortString];
  unsigned char len = 0;
  if (!reader_.Read(name_ptr, &len, 1)) return false;
  if (len == 0) return false;
  if (!reader_.Read(static_cast<const unsigned char *>(name_ptr) + 1, raw, len)) return false;
  for (unsigned char i = 0; i < len; ++i) {
    if (!Printable(raw[i])) return false;
  }
  std::size_t copy = len < out_size - 1 ? len : out_size - 1;
  memcpy(out, raw, copy);
  out[copy] = '\0';
  return true;
}

std::size_t Runtime::InstanceSizeOf(const void *obj) const {
  char name[64];
  if (!ClassNameOf(obj, name, sizeof(name))) return 0;
  const void *vmt = nullptr;
  if (!reader_.Read(obj, &vmt, sizeof(vmt))) return 0;
  uint64_t instance_size = 0;
  if (!reader_.Read(static_cast<const unsigned char *>(vmt) + kVmtInstanceSize, &instance_size,
                    sizeof(instance_size))) {
    return 0;
  }
  return static_cast<std::size_t>(instance_size);
}

std::size_t Runtime::HexAt(const void *src, std::size_t n, char *out, std::size_t out_size) const {
  if (out == nullptr || out_size == 0) return 0;
  out[0] = '\0';
  if (n == 0 || n > SafeReader::kMaxRead) return 0;
  std::size_t max_bytes = (out_size - 1) / 2;
  if (n > max_bytes) n = max_bytes;
  if (n == 0) return 0;
  unsigned char buf[SafeReader::kMaxRead];
  if (!reader_.Read(src, buf, n)) return 0;
  static const char kHex[] = "0123456789abcdef";
  for (std::size_t i = 0; i < n; ++i) {
    out[i * 2] = kHex[(buf[i] >> 4) & 0xF];
    out[i * 2 + 1] = kHex[buf[i] & 0xF];
  }
  out[n * 2] = '\0';
  return n;
}

}  // namespace fl_pascal
