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

# Optional trailing args assert WHICH method sits in a given slot, as
# "<slot>=<MethodName>". That is the second load-bearing property: FL 2026
# indexes this object as a Free Pascal class, so its VMT header pushes the SDK
# virtuals to slot 25 onward (native/abi/fl_host_abi.h). If a method is added,
# removed or reordered above slot 25, every host->plugin call silently shifts by
# one and lands on the wrong handler - which no data static_assert can catch.
DYLIB="${1:?usage: check_vtable.sh <dylib> <class> [slot=Method ...]}"
CLASS="${2:?usage: check_vtable.sh <dylib> <class> [slot=Method ...]}"
shift 2 || true
SLOT_ASSERTS="$*"
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

    [ -n "$SLOT_ASSERTS" ] || continue
    if ! command -v python3 >/dev/null 2>&1; then
        echo "check_vtable[$arch]: python3 unavailable, slot assertions skipped"
        continue
    fi
    thin="$(mktemp -t vtslice)"
    trap 'rm -f "$thin"' EXIT
    lipo -thin "$arch" "$DYLIB" -output "$thin" 2>/dev/null || cp "$DYLIB" "$thin"
    python3 - "$thin" "$VT" $SLOT_ASSERTS <<'PYEOF' || exit 1
import re, struct, subprocess, sys

path, vt_name, *asserts = sys.argv[1:]
syms = {}
for line in subprocess.run(["nm", "-n", path], capture_output=True, text=True).stdout.splitlines():
    parts = line.split(None, 2)
    if len(parts) == 3 and parts[0].strip():
        try:
            syms[int(parts[0], 16)] = parts[2]
        except ValueError:
            pass
blob = open(path, "rb").read()
vt = next((a for a, n in syms.items() if n == vt_name), None)
if vt is None:
    print(f"check_vtable: {vt_name} absent from the thin slice, slot assertions skipped")
    sys.exit(0)

def method_at(slot):
    # +16 skips the Itanium offset-to-top and typeinfo words. The stored value
    # carries chained-fixup tag bits in the high half; the low 32 bits are the
    # in-image address.
    raw = struct.unpack_from("<Q", blob, vt + 16 + slot * 8)[0] & 0xFFFFFFFF
    return syms.get(raw, "<unresolved>")

bad = 0
for spec in asserts:
    slot_s, _, want = spec.partition("=")
    slot = int(slot_s)
    got = method_at(slot)
    # Itanium mangling is _ZN<len>Bridge<len><Method>E<params>, so an exact
    # match is the length-prefixed name followed by the nested-name terminator.
    if re.search(r"(?<!\d)" + str(len(want)) + re.escape(want) + r"E", got):
        print(f"check_vtable: OK slot {slot} (VMT byte {200 + (slot - 25) * 8 if slot >= 25 else slot * 8}) == {want}")
    else:
        print(f"check_vtable: FAIL slot {slot} should be {want}, found {got}", file=sys.stderr)
        bad += 1
if bad:
    print("  FL 2026 calls this object at VMT byte 200 + 8*i (FPC VMT header);", file=sys.stderr)
    print("  a shifted vtable sends every host call to the wrong method.", file=sys.stderr)
    sys.exit(1)
PYEOF
    rm -f "$thin"
done

if [ "$checked_any" -eq 0 ]; then
    echo "check_vtable: no slice yielded $VT (skip)"
fi
exit 0
