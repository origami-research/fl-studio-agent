// The FL Studio 2026 (macOS) plugin -> host call ABI, and why it is NOT the one
// the C++ SDK header implies.
//
// FL Studio 2026 for macOS is built with Free Pascal. `TFruityPlugHost` and
// `TFruityPlug` are FPC *classes*, and an FPC VMT begins with a fixed header
// (vInstanceSize, vInstanceSize2, vParent, vClassName, ... , the inherited
// TObject method pointers) BEFORE the first method the class itself declares.
// A C++ `host->Dispatcher(...)` resolves to vtable slot 0, which on this host
// is vInstanceSize - a size, not a function. Branching there kills FL on the
// FIRST host call, whatever the payload: no note data, note count, Vol value or
// target is involved, because FL never gets to look at the buffer.
//
// Evidence, all static and read-only, from FL Studio 2026 Producer 26.1.5 build
// 5384, arm64 slice (reproduce with the lipo recipe in BRIDGE_FINDINGS.md):
//
//  1. FLEngine_x64.dylib carries the real FPC VMTs - `TFruityPlug` @0x13f7c88
//     and `TFruityPlugHost` @0x13f7df8. Both are self-identifying: +0 is the
//     instance size (0xa4 / 0x100), +8 is exactly its two's complement, +16 is
//     the same parent VMT, and +24 points at the class-name shortstring. Every
//     entry from +96 to +192 is byte-identical between the two classes (the
//     inherited TObject methods); +200 is the first entry that differs, and on
//     TFruityPlugHost every slot from +200 on is FPC's abstract-method stub -
//     which is exactly what a class of pure virtuals looks like.
//
//  2. Fruity Convolver - Image-Line's own shipping FL 2026 Fruity EFFECT, the
//     same plugin category as the Bridge - calls its host like this:
//         ldr x5, [x19, #0xc0]   ; its PlugHost field
//         ldr x5, [x5]           ; the VMT
//         ldr x5, [x5, #0xc8]    ; +200 = TFruityPlugHost.Dispatcher
//         blr x5                 ; x0=Self x1=[plug+8]=HostTag x2=ID x3=Index x4=Value
//     Twenty distinct FHD_* ids (2, 13, 14, 19, 22, 26, 28, 29, 33, 40, 44, 46,
//     48, 59, 61, 71, 85, 86, 91, 93, 94, 95) go through that one offset. The
//     argument shape is exactly what the C++ header declares - only the slot
//     moves.
//
//  3. FL calls plugins through the same shift, which is what the "extended
//     vtable slot" saga in BRIDGE_FINDINGS.md was actually seeing: our own
//     plugin's vtable byte 208 was handed FPD_SetBlockSize(128) and
//     FPD_SetSampleRate(44100), byte 216 is the steady idle tick, and byte 256
//     gets (source, dest, length) with source == dest. Those are the SDK's
//     Dispatcher, Idle_Public and Eff_Render - virtuals #1, #2 and #7 - each
//     displaced by the same 200 bytes.
//
// DATA layout is NOT shifted. `TFruityPlug.HostTag` is still at +8 on both
// sides (Convolver loads Sender from [plug+8]), and the note-params records
// still match the header (BRIDGE_FINDINGS.md, "the FL 2026 note-params layout,
// from FL's own RTTI"). Only method indexing moves - so this header changes how
// we CALL the host and nothing else.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

#include "fp_plugclass.h"

namespace fl_abi {

// One entry of the FPC VMT header. `verified` records how the slot was pinned
// against FL's own binary, so a future reader can tell decoded fact from
// convention: the header's SIZE is what this contract depends on, and that is
// measured, not assumed.
struct FpcVmtSlot {
  std::size_t offset;
  const char *name;
  bool verified;  // true = read out of FL's VMTs; false = FPC convention, unused here
};

// FPC's TObject VMT header as it appears in FLEngine_x64.dylib. The five
// `verified` entries were read directly out of both class VMTs; the rest are
// the inherited TObject method pointers, verified only as "byte-identical in
// both classes, therefore inherited, therefore part of the header". That is
// all this contract needs - it never calls one, it only steps over them.
inline constexpr FpcVmtSlot kFpcVmtHeader[] = {
    {0, "vInstanceSize", true},         // 0xa4 (TFruityPlug) / 0x100 (TFruityPlugHost)
    {8, "vInstanceSize2", true},        // exactly -vInstanceSize in both
    {16, "vParent", true},              // both -> the same TObject VMT
    {24, "vClassName", true},           // -> "TFruityPlug" / "TFruityPlugHost" shortstring
    {32, "vDynamicTable", false},   {40, "vMethodTable", false},
    {48, "vFieldTable", false},
    {56, "vTypeInfo", true},            // per-class, points into FL's RTTI
    {64, "vInitTable", false},      {72, "vAutoTable", false},
    {80, "vIntfTable", false},      {88, "vMsgStrPtr", false},
    {96, "vDestroy", false},        {104, "vNewInstance", false},
    {112, "vFreeInstance", false},  {120, "vSafeCallException", false},
    {128, "vDefaultHandler", false},{136, "vAfterConstruction", false},
    {144, "vBeforeDestruction", false}, {152, "vDefaultHandlerStr", false},
    {160, "vDispatch", false},      {168, "vDispatchStr", false},
    {176, "vEquals", false},        {184, "vGetHashCode", false},
    {192, "vToString", false},
};

inline constexpr std::size_t kFpcVmtHeaderSlots =
    sizeof(kFpcVmtHeader) / sizeof(kFpcVmtHeader[0]);

// Bytes of VMT header in front of a class's first declared virtual method.
inline constexpr std::size_t kFpcVmtHeaderBytes = kFpcVmtHeaderSlots * sizeof(void *);
static_assert(kFpcVmtHeaderBytes == 200, "FL 2026's FPC VMT header is 200 bytes (25 slots)");

// Byte offset of the host's Nth declared virtual method. `TFruityPlugHost`
// declares Dispatcher first (fp_plugclass.h), so Dispatcher is method 0.
inline constexpr std::size_t HostMethodOffset(std::size_t method_index) {
  return kFpcVmtHeaderBytes + method_index * sizeof(void *);
}
inline constexpr std::size_t kHostDispatcherOffset = HostMethodOffset(0);
static_assert(kHostDispatcherOffset == 200, "TFruityPlugHost.Dispatcher is at VMT+200");

// Self is passed in the first argument slot on both sides, so the only thing
// that differs from the C++ declaration is which VMT entry we load.
using HostDispatcherFn = std::intptr_t (*)(void *self, std::intptr_t sender, std::intptr_t id,
                                           std::intptr_t index, std::intptr_t value);

// THE one way this codebase calls FL. `host->Dispatcher(...)` must never appear
// again: it compiles to VMT slot 0 and takes the DAW down.
inline std::intptr_t HostDispatch(TFruityPlugHost *host, std::intptr_t sender, std::intptr_t id,
                                  std::intptr_t index, std::intptr_t value) {
  // Read the object's first word as bytes. FL's host is an FPC object, not the
  // C++ class this pointer is typed as, so the read is deliberately untyped -
  // casting through unsigned char* also keeps clang from warning that we are
  // memcpy-ing out of a "dynamic class".
  const unsigned char *const object = reinterpret_cast<const unsigned char *>(host);
  const unsigned char *vmt = nullptr;
  std::memcpy(&vmt, object, sizeof vmt);  // FPC objects keep the VMT pointer at +0
  HostDispatcherFn dispatcher = nullptr;
  std::memcpy(&dispatcher, vmt + kHostDispatcherOffset, sizeof dispatcher);
  return dispatcher(host, sender, id, index, value);
}

}  // namespace fl_abi
