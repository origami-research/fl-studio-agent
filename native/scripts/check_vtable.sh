#!/usr/bin/env bash
# ABI invariant check for a plugin dylib: assert its vtable has EXACTLY 64
# slots (base-free, no virtual destructor) - the load-bearing property of the
# proven FL 2026 ABI (native/bridge/BRIDGE_FINDINGS.md) that the compile-time
# data-layout static_asserts do NOT cover. If a future edit drops or adds a
# slot, the build's `ctest` fails here instead of FL crashing on insert.
#
# Technique: the Itanium vtable object `_ZTV<len><Class>` is laid out as
# [offset-to-top][typeinfo ptr] then one pointer per vtable slot. So its byte
# size = (2 + slots) * 8. We measure that size as the gap to the next symbol in
# address order, per architecture slice.
#
# macOS/Mach-O only. On any other platform, or if the symbol is stripped/absent,
# the check prints why and exits 0 so ctest stays green cross-platform - the
# real gate is a macOS build, which is where FL runs.
set -euo pipefail

DYLIB="${1:?usage: check_vtable.sh <dylib> <class>}"
CLASS="${2:?usage: check_vtable.sh <dylib> <class>}"
if [ ! -f "$DYLIB" ]; then
    echo "check_vtable: no dylib at $DYLIB (skip)"
    exit 0
fi
if ! command -v nm >/dev/null 2>&1 || ! command -v lipo >/dev/null 2>&1; then
    echo "check_vtable: nm/lipo unavailable (skip)"
    exit 0
fi

EXPECT_SLOTS=64
EXPECT_BYTES=$(( (2 + EXPECT_SLOTS) * 8 ))   # 66 words = 528 bytes = 0x210
# Itanium mangling for a vtable in the global namespace, with the leading
# underscore Mach-O prepends: _ZTV<name-length><name>.
VT="__ZTV${#CLASS}${CLASS}"

checked_any=0
for arch in $(lipo -archs "$DYLIB" 2>/dev/null || true); do
    # "<hex-addr> <name>" per line, address-sorted ascending (nm -n). BSD awk on
    # macOS has no strtonum, so the hex math is done in bash below.
    syms="$(nm -n -arch "$arch" "$DYLIB" 2>/dev/null | awk 'NF>=2 {print $1, $NF}' || true)"
    [ -n "$syms" ] || continue
    vaddr="$(printf '%s\n' "$syms" | awk -v vt="$VT" '$2==vt {print $1; exit}')"
    if [ -z "$vaddr" ]; then
        echo "check_vtable[$arch]: $VT not found (stripped? skip this slice)"
        continue
    fi
    # first symbol at a strictly greater address == end of the vtable object
    next="$(printf '%s\n' "$syms" | awk -v v="$vaddr" '$1==v{f=1;next} f && $1!=v{print $1; exit}')"
    if [ -z "$next" ]; then
        echo "check_vtable[$arch]: no symbol after $VT (skip this slice)"
        continue
    fi
    span=$(( 16#$next - 16#$vaddr ))
    checked_any=1
    if [ "$span" -ne "$EXPECT_BYTES" ]; then
        echo "check_vtable[$arch]: FAIL vtable is $span bytes, expected $EXPECT_BYTES ($EXPECT_SLOTS slots + 2 header words)" >&2
        echo "  the base-free 64-slot ABI drifted - FL 2026 host-calls extended slots and will crash if the vtable is short" >&2
        exit 1
    fi
    echo "check_vtable[$arch]: OK $span bytes == $EXPECT_SLOTS slots"
done

if [ "$checked_any" -eq 0 ]; then
    echo "check_vtable: no slice yielded $VT (skip)"
fi
exit 0
