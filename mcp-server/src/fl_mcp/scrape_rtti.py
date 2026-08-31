"""Static RTTI scraper: read FL's own binary for the facts the profile carries.

FLEngine_x64.dylib is compiled with Free Pascal and self-describes (docs/
research/r4-streamline-and-audit.md §A2, BRIDGE_FINDINGS.md "from FL's own
RTTI"):

- Record RTTI: a ``0x0d`` (tkRecord) kind byte + shortstring name appears
  TWICE per record. The second copy is the extended field table: after the
  name (8-aligned) comes a back-pointer to the first copy, then
  ``RecSize:u32, NumFields:u32`` and ``NumFields`` x ``(PPTypeInfo:u64,
  Offset:u64)``. Each PPTypeInfo double-derefs to the field's own typeinfo
  (kind byte + type-name shortstring). Field NAMES are not in the RTTI; the
  SDK's declaration-order labels are applied positionally.
- Class VMTs are self-identifying: ``+0`` instance size, ``+8`` its exact
  two's complement, ``+16`` parent VMT, ``+24`` class-name shortstring. The
  first slot where two sibling classes differ (scanning past the shared
  TObject method block) is the first DECLARED method - the "+200" fact.
- ``TFruityHost.Dispatcher`` (the concrete host's first declared method)
  opens with a ``cmp x0, #imm; b.hi`` guard in front of a jump table; the
  immediate is the handled-id bound (109 on FL 2026). Pattern-anchored and
  refuses to guess: an unrecognized prologue returns None, never a stale
  bound.

Pure Python, stdlib only; no FL running, no C++. This dylib's ``__TEXT``
vmaddr is 0, so every pointer stored in the image is also a file offset into
the arm64 slice - each one is validated in-range before use (fail closed,
never guess).

CLI: ``python -m fl_mcp.scrape_rtti --dylib <path> [--merge profile.json]``.
``--merge`` updates only the scraper-owned blocks (meta hash/arch/sources,
structs, dispatcher addresses/offsets/bound) and never the live-probe-owned
ones (unsafe ids, getters, push_events, tick, capabilities) - the two sources
compose into one profile without clobbering each other.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .profile import StructField, StructLayout, profile_to_dict

TK_RECORD = 0x0D
TK_ARRAY = 0x0C

# RTTI carries no field names; these are the SDK's declaration-order labels
# (native/sdk/fp_plugclass.h), applied positionally. A record whose scraped
# field COUNT disagrees gets positional names instead - which makes the golden
# test fail loudly naming the extra/missing field, exactly the alarm we want.
KNOWN_FIELD_NAMES: dict[str, tuple[str, ...]] = {
    "TNoteParams": ("Position", "Length", "Pan", "Vol", "Note", "Color", "Pitch", "FCut", "FRes"),
    "TNotesParams": ("Target", "Flags", "PatNum", "ChanNum", "Count", "NoteParams"),
    "TFruityPlugInfo": (
        "SDKVersion",
        "LongName",
        "ShortName",
        "Flags",
        "NumParams",
        "DefPoly",
        "NumOutCtrls",
        "NumOutVoices",
        "Reserved",
    ),
}

STRUCT_NAMES = tuple(KNOWN_FIELD_NAMES)
CLASS_NAMES = ("TFruityPlug", "TFruityPlugHost", "TFruityHost")

# Mach-O constants (stdlib struct only - no macholib dependency).
_MH_MAGIC_64 = 0xFEEDFACF
_FAT_MAGIC = 0xCAFEBABE
_CPU_TYPE_ARM64 = 0x0100000C

# Sanity bounds: anything past these is not the structure we think it is.
_MAX_FIELDS = 512
_MAX_INSTANCE_SIZE = 0x100000
_MAX_GUARD_SCAN_INSNS = 256
# The shared TObject method block starts here in an FPC VMT; header fields
# below it (vTypeInfo etc.) legitimately differ per class.
_VMT_METHODS_START = 96
_VMT_SCAN_LIMIT = 4096


class ScrapeError(RuntimeError):
    """The image does not carry the structure we expected - report, don't guess."""


def _u32(image: bytes, off: int) -> int:
    return struct.unpack_from("<I", image, off)[0]


def _u64(image: bytes, off: int) -> int:
    return struct.unpack_from("<Q", image, off)[0]


def _in_image(image: bytes, off: int, need: int = 1) -> bool:
    return off >= 0 and off + need <= len(image)


def _shortstring(image: bytes, off: int) -> str | None:
    """Pascal shortstring: length byte + bytes. None if unreadable/unprintable."""
    if not _in_image(image, off, 1):
        return None
    n = image[off]
    if not _in_image(image, off + 1, n):
        return None
    raw = image[off + 1 : off + 1 + n]
    if any(c < 0x20 or c >= 0x7F for c in raw):
        return None
    return raw.decode("ascii")


# --- slice selection ---------------------------------------------------------


def arm64_slice(dylib: Path) -> bytes:
    """The arm64 Mach-O slice of a (possibly fat) dylib, in memory."""
    return _arm64_slice_bytes(dylib.read_bytes())


def _arm64_slice_bytes(data: bytes) -> bytes:
    if len(data) < 8:
        raise ScrapeError("file too small to be a Mach-O image")
    if struct.unpack_from(">I", data, 0)[0] == _FAT_MAGIC:
        (nfat,) = struct.unpack_from(">I", data, 4)
        for i in range(nfat):
            cputype, _sub, offset, size, _align = struct.unpack_from(">IIIII", data, 8 + 20 * i)
            if cputype == _CPU_TYPE_ARM64:
                return data[offset : offset + size]
        raise ScrapeError("fat binary has no arm64 slice")
    if struct.unpack_from("<I", data, 0)[0] == _MH_MAGIC_64:
        (cputype,) = struct.unpack_from("<I", data, 4)
        if cputype != _CPU_TYPE_ARM64:
            raise ScrapeError(f"thin Mach-O is not arm64 (cputype {cputype:#x})")
        return data
    raise ScrapeError("not a Mach-O image (bad magic)")


# --- record RTTI -------------------------------------------------------------


def _typeinfo_hits(image: bytes, name: str) -> list[int]:
    """Offsets of every `0x0d + shortstring(name)` typeinfo entry."""
    pattern = bytes([TK_RECORD, len(name)]) + name.encode("ascii")
    hits = []
    start = 0
    while (found := image.find(pattern, start)) >= 0:
        hits.append(found)
        start = found + 1
    return hits


def _type_name(image: bytes, typeinfo_off: int) -> str:
    """A field's type from its typeinfo entry: the type-name shortstring,
    falling back to the kind for anonymous types (inline arrays)."""
    kind = image[typeinfo_off]
    name = _shortstring(image, typeinfo_off + 1)
    if name:
        return name
    if kind == TK_ARRAY:
        return "array"
    return f"kind_{kind:#04x}"


def scrape_structs(image: bytes, names: tuple[str, ...] = STRUCT_NAMES) -> dict[str, StructLayout]:
    """Walk the FPC extended record RTTI for each named record.

    Raises ScrapeError when a requested record has no decodable extended
    field table - a missing layout must fail the run, not thin the profile.
    """
    out: dict[str, StructLayout] = {}
    for name in names:
        hits = _typeinfo_hits(image, name)
        layout = None
        for hit in hits:
            layout = _decode_extended(image, name, hit, set(hits))
            if layout is not None:
                break
        if layout is None:
            raise ScrapeError(f"no decodable extended RTTI field table for record {name!r}")
        out[name] = layout
    return out


def _decode_extended(image: bytes, name: str, hit: int, all_hits: set[int]) -> StructLayout | None:
    """Decode one candidate entry as the EXTENDED copy; None if it is not one."""
    table = (hit + 2 + len(name) + 7) & ~7  # 8-align past kind byte + shortstring
    if not _in_image(image, table, 16):
        return None
    back = _u64(image, table)
    if back == hit or back not in all_hits:
        return None  # the base copy (or noise), not the extended table
    rec_size = _u32(image, table + 8)
    num_fields = _u32(image, table + 12)
    if rec_size == 0 or num_fields == 0 or num_fields > _MAX_FIELDS:
        return None
    labels = KNOWN_FIELD_NAMES.get(name, ())
    if len(labels) != num_fields:
        labels = tuple(f"field{i}" for i in range(num_fields))
    fields = []
    for i in range(num_fields):
        entry = table + 16 + 16 * i
        if not _in_image(image, entry, 16):
            return None
        pp, offset = _u64(image, entry), _u64(image, entry + 8)
        if offset >= rec_size or not _in_image(image, pp, 8):
            return None
        typeinfo = _u64(image, pp)
        if not _in_image(image, typeinfo, 2):
            return None
        fields.append(StructField(name=labels[i], offset=offset, type=_type_name(image, typeinfo)))
    return StructLayout(size=rec_size, fields=tuple(fields))


# --- class VMTs --------------------------------------------------------------


@dataclass(frozen=True)
class VmtFacts:
    address: int  # offset of the VMT in the arm64 slice
    instance_size: int
    parent_addr: int


def scrape_vmts(image: bytes, class_names: tuple[str, ...] = CLASS_NAMES) -> dict[str, VmtFacts]:
    """Locate each class VMT by its class-name shortstring back-reference.

    A candidate is only accepted when it self-identifies: ``+0`` a plausible
    instance size, ``+8`` its exact two's complement, ``+24`` the name.
    Missing or ambiguous classes raise - never guess between two VMTs.
    """
    out: dict[str, VmtFacts] = {}
    for name in class_names:
        candidates = set()
        pattern = bytes([len(name)]) + name.encode("ascii")
        start = 0
        while (s := image.find(pattern, start)) >= 0:
            start = s + 1
            ref = struct.pack("<Q", s)
            ref_start = 0
            while (r := image.find(ref, ref_start)) >= 0:
                ref_start = r + 1
                vmt = r - 24
                if vmt < 0 or not _in_image(image, vmt, 32):
                    continue
                size = _u64(image, vmt)
                neg = _u64(image, vmt + 8)
                if 0 < size <= _MAX_INSTANCE_SIZE and (size + neg) & 0xFFFFFFFFFFFFFFFF == 0:
                    candidates.add(vmt)
        if not candidates:
            raise ScrapeError(f"no self-identifying VMT found for class {name!r}")
        if len(candidates) > 1:
            raise ScrapeError(f"ambiguous VMTs for class {name!r}: {sorted(map(hex, candidates))}")
        vmt = candidates.pop()
        out[name] = VmtFacts(address=vmt, instance_size=_u64(image, vmt), parent_addr=_u64(image, vmt + 16))
    return out


def first_method_offset(image: bytes, vmt_a: int, vmt_b: int) -> int:
    """The byte offset of a class's first DECLARED method slot: where two
    sibling classes' VMTs first differ past the shared TObject method block.

    Both differing slots must hold in-image pointers (method code or FPC's
    abstract stub); a difference in anything else is not the method boundary.
    On FL 2026 (TFruityPlug vs TFruityPlugHost) this is 200.
    """
    for off in range(_VMT_METHODS_START, _VMT_SCAN_LIMIT, 8):
        if not (_in_image(image, vmt_a + off, 8) and _in_image(image, vmt_b + off, 8)):
            break
        a, b = _u64(image, vmt_a + off), _u64(image, vmt_b + off)
        if a != b:
            if _in_image(image, a) and _in_image(image, b):
                return off
            raise ScrapeError(
                f"VMTs first differ at +{off} but the slots are not code pointers "
                f"({a:#x} / {b:#x}) - not a method boundary"
            )
    raise ScrapeError("VMTs never diverge - cannot locate the first declared method slot")


# --- dispatcher guard --------------------------------------------------------


def _iter_guard(image: bytes, dispatcher_addr: int):
    """Yield (index, cmp_imm, insn_offset) for each `cmp x0,#imm; b.hi` pair
    followed by a jump-table `br` within a few instructions."""
    for k in range(_MAX_GUARD_SCAN_INSNS):
        off = dispatcher_addr + 4 * k
        if not _in_image(image, off, 12):
            return
        insn = _u32(image, off)
        # cmp x0, #imm12 (unshifted) == subs xzr, x0, #imm12
        if (insn & 0xFFC003FF) != 0xF100001F:
            continue
        if (_u32(image, off + 4) & 0xFF00001F) != 0x54000008:  # b.hi
            continue
        # jump-table dispatch: a `br xN` within the next 8 instructions
        for j in range(2, 10):
            if not _in_image(image, off + 4 * j, 4):
                break
            if (_u32(image, off + 4 * j) & 0xFFFFFC1F) == 0xD61F0000:  # br xN
                yield k, (insn >> 10) & 0xFFF, off
                break


def detect_dispatcher_bound(image: bytes, dispatcher_addr: int) -> int | None:
    """Decode the `cmp x0, #imm; b.hi` guard in front of the dispatcher's jump
    table; the immediate is the highest handled id (109 on FL 2026).

    Pattern-anchored and refuses to guess: returns None when no such guard
    exists in the prologue - the discovery job then reports "bound unknown -
    sweep the full range with quarantine" instead of keeping a stale bound.
    """
    for _k, imm, _off in _iter_guard(image, dispatcher_addr):
        return imm
    return None


def detect_jump_table(image: bytes, dispatcher_addr: int) -> int | None:
    """The jump table's address, from the `adrp; add` pair between the guard
    and the `br`. Informational; None when the shape is unrecognized."""
    for _k, _imm, off in _iter_guard(image, dispatcher_addr):
        page = None
        for j in range(2, 10):
            if not _in_image(image, off + 4 * j, 4):
                return None
            insn = _u32(image, off + 4 * j)
            if (insn & 0x9F000000) == 0x90000000:  # adrp
                immlo = (insn >> 29) & 0x3
                immhi = (insn >> 5) & 0x7FFFF
                imm = (immhi << 2) | immlo
                if imm & (1 << 20):
                    imm -= 1 << 21
                page = (((off + 4 * j) >> 12) << 12) + (imm << 12)
            elif page is not None and (insn & 0xFFC00000) == 0x91000000:  # add xN, xN, #imm
                return page + ((insn >> 10) & 0xFFF)
            elif (insn & 0xFFFFFC1F) == 0xD61F0000:  # br - shape ended without add
                return None
        return None
    return None


# --- the full scrape ---------------------------------------------------------


@dataclass(frozen=True)
class ScrapeResult:
    dylib_sha256: str  # of the dylib file as shipped (the fat container)
    structs: dict[str, StructLayout]
    vmts: dict[str, VmtFacts]
    vmt_first_method_offset: int
    host_dispatcher_addr: int
    handled_max_id: int | None
    jump_table_addr: int | None


def scrape(dylib: Path) -> ScrapeResult:
    data = dylib.read_bytes()
    image = _arm64_slice_bytes(data)
    structs = scrape_structs(image)
    vmts = scrape_vmts(image)
    offset = first_method_offset(image, vmts["TFruityPlug"].address, vmts["TFruityPlugHost"].address)
    dispatcher_addr = _u64(image, vmts["TFruityHost"].address + offset)
    if not _in_image(image, dispatcher_addr, 4):
        raise ScrapeError(f"TFruityHost first-method slot is not in-image: {dispatcher_addr:#x}")
    return ScrapeResult(
        dylib_sha256=hashlib.sha256(data).hexdigest(),
        structs=structs,
        vmts=vmts,
        vmt_first_method_offset=offset,
        host_dispatcher_addr=dispatcher_addr,
        handled_max_id=detect_dispatcher_bound(image, dispatcher_addr),
        jump_table_addr=detect_jump_table(image, dispatcher_addr),
    )


def merge_scrape(doc: dict, result: ScrapeResult, generated: str) -> dict:
    """A new profile document with ONLY the scraper-owned blocks replaced.

    Scraper-owned: meta.{engine_dylib_sha256, arch, generated, sources},
    structs, dispatcher.{vmt_first_method_offset, host_dispatcher_addr,
    jump_table_addr, handled_max_id}. Live-probe-owned blocks (unsafe ids,
    inert_in_range, getters, push_events, tick, capabilities) and the format
    blocks (flp, fst, ppq, spool_roots) are preserved verbatim.
    handled_max_id / jump_table_addr are left untouched when undetected - the
    caller reports that, this function never erases a fact.
    """
    merged = json.loads(json.dumps(doc))  # deep copy; inputs stay immutable
    meta = merged.setdefault("meta", {})
    meta["engine_dylib_sha256"] = result.dylib_sha256
    meta["arch"] = "arm64"
    meta["generated"] = generated
    meta["sources"] = sorted(set(meta.get("sources", [])) | {"rtti", "vmt"})
    merged["structs"] = {
        name: {
            "size": layout.size,
            "fields": [{"name": f.name, "offset": f.offset, "type": f.type} for f in layout.fields],
        }
        for name, layout in result.structs.items()
    }
    dispatcher = merged.setdefault("dispatcher", {})
    dispatcher["vmt_first_method_offset"] = result.vmt_first_method_offset
    dispatcher["host_dispatcher_addr"] = hex(result.host_dispatcher_addr)
    if result.handled_max_id is not None:
        dispatcher["handled_max_id"] = result.handled_max_id
    if result.jump_table_addr is not None:
        dispatcher["jump_table_addr"] = hex(result.jump_table_addr)
    return merged


def _default_dylib() -> Path:
    from .fl.paths import fl_app

    return fl_app() / "Contents" / "Libs" / "FLEngine_x64.dylib"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fl_mcp.scrape_rtti",
        description="Scrape struct layouts, VMT facts and the dispatcher bound from FLEngine.",
    )
    parser.add_argument("--dylib", type=Path, default=None, help="FLEngine_x64.dylib (default: the installed FL app's)")
    parser.add_argument("--merge", type=Path, default=None, help="profile.json to update in place (scraper-owned blocks only)")
    args = parser.parse_args(argv)

    dylib = args.dylib or _default_dylib()
    if not dylib.is_file():
        print(f"error: no dylib at {dylib}", file=sys.stderr)
        return 1
    try:
        result = scrape(dylib)
    except (ScrapeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result.handled_max_id is None:
        print(
            "warning: dispatcher guard unrecognized - bound unknown; sweep the full "
            "range with quarantine instead of trusting a previous bound",
            file=sys.stderr,
        )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.merge is not None:
        try:
            doc = json.loads(args.merge.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read profile {args.merge}: {exc}", file=sys.stderr)
            return 1
        merged = merge_scrape(doc, result, generated)
        args.merge.write_text(json.dumps(merged, indent=2) + "\n")
        print(f"merged scrape of {dylib} into {args.merge}")
        return 0

    # No profile to merge into: print the scraper-owned blocks as JSON.
    from .profile import BUILTIN_FALLBACK

    skeleton = profile_to_dict(BUILTIN_FALLBACK)
    print(json.dumps(merge_scrape(skeleton, result, generated), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
