// Fault-free reads of host memory, and FPC class identification on top of them.
//
// WHY THIS EXISTS. Every interesting discovery result is an intptr_t. A sweep
// that records `ret=6104883424` has learned almost nothing; a sweep that
// records `ret_class="TPublicChannel"` has answered the question the whole
// pass is about. FL Studio 2026 for macOS is Free Pascal (fl_host_abi.h), so
// every object FL might hand back is an FPC class instance, and an FPC class
// is self-describing: the object's first word is its VMT, and the VMT carries
// the instance size at +0, the parent VMT at +16 and a `vClassName`
// shortstring pointer at +24. Decoding that turns opaque integers into named
// objects. The Bridge's wp_* discovery ops (bridge_core) use this to enrich
// their results; it began life in the throwaway probe and moved here when the
// probe's passes were folded into the Bridge.
//
// THE SAFETY INVARIANT. A returned integer may be any bit pattern at all, so
// dereferencing it directly would crash FL - the exact failure this facility
// exists to avoid. So this header has ONE memory primitive, `SafeRead`, and
// nothing here ever dereferences a host pointer. SafeRead hands the address to
// the kernel (a write into a pipe) instead of the CPU: an unmapped or
// unreadable source comes back as EFAULT, a false return and an untouched
// destination. No signal, no handler, no crash - and no reliance on the
// process's own fault handling, which on this host is FPC setjmp/longjmp that
// would unwind straight past our frames (BRIDGE_FINDINGS.md).
//
// SCOPE. Read-only by construction: there is no write primitive here.
#pragma once

#include <cstddef>

namespace fl_pascal {

// A kernel-mediated reader. Holds one pipe for the lifetime of the object; the
// Bridge drives it from the GUI-thread pump only, so it needs no locking (and
// takes none - a lock on FL's main thread is the one thing this codebase must
// never add).
class SafeReader {
 public:
  SafeReader();
  ~SafeReader();
  SafeReader(const SafeReader &) = delete;
  SafeReader &operator=(const SafeReader &) = delete;

  bool valid() const { return fds_[0] >= 0; }

  // Copies `n` bytes from `src` into `dst`. Returns false - leaving `dst`
  // untouched - if `src` is null, `n` exceeds kMaxRead, the reader could not be
  // constructed, or the source range is not readable. Never faults.
  bool Read(const void *src, void *dst, std::size_t n) const;

  // The largest single read. Bounded well below the smallest guaranteed pipe
  // buffer so one write/read pair is atomic and never blocks.
  static constexpr std::size_t kMaxRead = 512;

 private:
  int fds_[2] = {-1, -1};
};

// The FPC class-identification runtime for one loaded image (FL's engine).
//
// An address only counts as a VMT if it lies inside that image, which is what
// makes "is this pointer an object?" a real test rather than a hopeful cast:
// a random integer essentially never points at a well-formed VMT inside the
// engine's own mapping.
class Runtime {
 public:
  // Production: derive the image from any address known to live inside it -
  // the TFruityPlugHost pointer FL hands the plugin is exactly such an address.
  explicit Runtime(const void *addr_in_image);
  // Tests: an explicitly injected image range, so class decoding is provable
  // offline against a hand-built VMT with no FL and no dynamic loader.
  Runtime(const void *image_base, std::size_t image_size);

  bool valid() const { return base_ != nullptr && size_ > 0 && reader_.valid(); }
  const void *image_base() const { return base_; }
  std::size_t image_size() const { return size_; }
  // Path of the resolved image, or "" when the range was injected.
  const char *image_path() const { return path_; }

  bool InImage(const void *addr) const;

  // Decodes `obj` as an FPC class instance and copies its class name into
  // `out` (always NUL-terminated when true). Returns false for anything that
  // does not decode cleanly: unreadable memory, a VMT outside the image, an
  // implausible instance size, a name pointer outside the image, or a name
  // that is not printable ASCII. False means "not identifiable", never
  // "probably fine".
  bool ClassNameOf(const void *obj, char *out, std::size_t out_size) const;

  // The instance size the object's VMT declares, or 0 if `obj` is not an
  // identifiable class instance.
  std::size_t InstanceSizeOf(const void *obj) const;

  // Lowercase hex of up to `n` bytes at `src` (n <= SafeReader::kMaxRead).
  // Writes "" and returns 0 when the source is unreadable, so an unreadable
  // pointer is recorded as such instead of as zeroes.
  std::size_t HexAt(const void *src, std::size_t n, char *out, std::size_t out_size) const;

  const SafeReader &reader() const { return reader_; }

 private:
  const void *base_ = nullptr;
  std::size_t size_ = 0;
  char path_[512] = {0};
  SafeReader reader_;
};

// FPC VMT field offsets, from FL 2026's own VMTs (BRIDGE_FINDINGS.md, "The call
// contract"): +0 instance size, +8 its two's complement, +16 parent VMT, +24
// vClassName. Named here so the decode reads as the layout it is.
inline constexpr std::size_t kVmtInstanceSize = 0;
inline constexpr std::size_t kVmtParent = 16;
inline constexpr std::size_t kVmtClassName = 24;
// A Pascal shortstring is a length byte followed by that many characters.
inline constexpr std::size_t kMaxShortString = 255;
// No FL class is anywhere near this large; a "size" above it means the word we
// read was not an instance size, so the pointer was not an object.
inline constexpr std::size_t kMaxPlausibleInstanceSize = 1u << 22;

}  // namespace fl_pascal
