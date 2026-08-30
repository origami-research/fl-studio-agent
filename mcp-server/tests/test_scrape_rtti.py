"""RTTI scraper: golden against the installed FL, synthetic negatives without it.

The golden tests scrape the real FLEngine dylib at /Applications and require
every fact to reproduce the packaged profile exactly - this is the test that
catches the next Vol-is-Single with zero manual reverse engineering. They skip
cleanly on machines without FL.

The synthetic tests build tiny fake images carrying the same FPC structures
(record RTTI, self-identifying VMTs, the dispatcher guard) so the scraper's
reading - not replaying - is proven everywhere, including the negative: a
shifted field yields a different layout.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from fl_mcp import scrape_rtti as S
from fl_mcp.profile import StructField, StructLayout, packaged_profiles, parse_profile

FL_DYLIB = Path("/Applications/FL Studio 2026.app/Contents/Libs/FLEngine_x64.dylib")
PACKAGED_NAME = "fl-26.1.5-5384-arm64.json"

needs_fl = pytest.mark.skipif(not FL_DYLIB.is_file(), reason="no FL 2026 install to scrape")


# --- synthetic image builders ------------------------------------------------


def rtti_image(record_name: str, rec_size: int, fields: list[tuple[int, str, int]]) -> bytes:
    """A minimal image carrying one record's FPC RTTI: the base typeinfo entry,
    the extended field table (back-pointer, RecSize, NumFields, then
    (PPTypeInfo, Offset) pairs), and the field-type typeinfo entries."""
    buf = bytearray(b"\0" * 64)
    type_offs: dict[str, int] = {}
    for _off, tname, kind in fields:
        if tname not in type_offs:
            type_offs[tname] = len(buf)
            buf += bytes([kind, len(tname)]) + tname.encode()
    while len(buf) % 8:
        buf += b"\0"
    pp_offs: dict[str, int] = {}
    for tname, toff in type_offs.items():
        pp_offs[tname] = len(buf)
        buf += struct.pack("<Q", toff)
    base = len(buf)
    buf += bytes([S.TK_RECORD, len(record_name)]) + record_name.encode()
    buf += bytes([S.TK_RECORD, len(record_name)]) + record_name.encode()  # the extended copy
    while len(buf) % 8:
        buf += b"\0"
    buf += struct.pack("<Q", base)
    buf += struct.pack("<II", rec_size, len(fields))
    for offset, tname, _kind in fields:
        buf += struct.pack("<QQ", pp_offs[tname], offset)
    return bytes(buf)


NOTE_FIELDS = [
    (0, "LongInt", 0x01),
    (4, "LongInt", 0x01),
    (8, "LongInt", 0x01),
    (12, "Single", 0x04),
    (16, "SmallInt", 0x01),
    (18, "SmallInt", 0x01),
    (20, "LongInt", 0x01),
    (24, "Single", 0x04),
    (28, "Single", 0x04),
]


def vmt_image(first_diff_offset: int = 200) -> tuple[bytes, int, int]:
    """An image with two sibling class VMTs (TFruityPlug / TFruityPlugHost)
    that share every slot below `first_diff_offset` and differ from there.
    Returns (image, vmt_a_offset, vmt_b_offset)."""
    buf = bytearray(b"\0" * 64)
    names = {}
    for name in ("TFruityPlug", "TFruityPlugHost"):
        names[name] = len(buf)
        buf += bytes([len(name)]) + name.encode()
    while len(buf) % 8:
        buf += b"\0"

    shared_ptr = 8  # any in-image address: the inherited TObject method block
    method_a, method_b = 16, 24  # distinct in-image "code" addresses

    def build(name: str, size: int, method: int) -> int:
        vmt = len(buf)
        buf.extend(struct.pack("<QQQQ", size, (1 << 64) - size, 0, names[name]))
        for off in range(32, first_diff_offset, 8):
            # header fields below 96 may differ per class; TObject block must not
            buf.extend(struct.pack("<Q", shared_ptr if off >= 32 else 0))
        buf.extend(struct.pack("<Q", method))  # the first declared method slot
        buf.extend(struct.pack("<Q", method))
        return vmt

    vmt_a = build("TFruityPlug", 0xA4, method_a)
    vmt_b = build("TFruityPlugHost", 0x100, method_b)
    return bytes(buf), vmt_a, vmt_b


def guard_code(bound: int, at: int = 0x100, prologue_insns: int = 12) -> bytes:
    """An image whose 'dispatcher' at `at` is a prologue of nops, then the
    `cmp x0,#bound; b.hi; adrp; add; ldrsw; br` jump-table shape FL uses."""
    nop = 0xD503201F
    insns = [nop] * prologue_insns
    insns.append(0xF100001F | (bound << 10))  # cmp x0, #bound
    insns.append(0x54000048)  # b.hi +8
    insns.append(0x90000001)  # adrp x1, . (immhi=immlo=0 -> current page)
    insns.append(0x91280021)  # add x1, x1, #0xa00
    insns.append(0xB8A07820)  # ldrsw x0, [x1, x0, lsl #2]
    insns.append(0xD61F0000)  # br x0
    body = b"".join(struct.pack("<I", i) for i in insns)
    return b"\0" * at + body


# --- synthetic: structs ------------------------------------------------------


def test_scrape_structs_synthetic():
    image = rtti_image("TNoteParams", 32, NOTE_FIELDS)
    layout = S.scrape_structs(image, ("TNoteParams",))["TNoteParams"]
    assert layout.size == 32
    assert layout.fields[3] == StructField(name="Vol", offset=12, type="Single")
    assert [f.name for f in layout.fields] == list(S.KNOWN_FIELD_NAMES["TNoteParams"])


def test_shifted_field_differs():
    """The negative: the scraper reads the image, it does not replay knowledge.
    A synthetic engine whose Vol moved to 16 produces a DIFFERENT layout."""
    straight = S.scrape_structs(rtti_image("TNoteParams", 32, NOTE_FIELDS), ("TNoteParams",))
    shifted_fields = [(o if o != 12 else 16, t, k) for o, t, k in NOTE_FIELDS]
    shifted = S.scrape_structs(rtti_image("TNoteParams", 32, shifted_fields), ("TNoteParams",))
    assert straight != shifted
    assert shifted["TNoteParams"].fields[3].offset == 16


def test_field_count_change_gets_positional_names():
    image = rtti_image("TNoteParams", 36, [*NOTE_FIELDS, (32, "LongInt", 0x01)])
    layout = S.scrape_structs(image, ("TNoteParams",))["TNoteParams"]
    assert [f.name for f in layout.fields] == [f"field{i}" for i in range(10)]


def test_missing_record_raises():
    with pytest.raises(S.ScrapeError, match="TNotesParams"):
        S.scrape_structs(rtti_image("TNoteParams", 32, NOTE_FIELDS), ("TNotesParams",))


def test_anonymous_array_type_named_array():
    image = rtti_image("TNotesParams", 52, [(0, "LongInt", 0x01), (20, "", S.TK_ARRAY)])
    layout = S.scrape_structs(image, ("TNotesParams",))["TNotesParams"]
    assert layout.fields[1].type == "array"


# --- synthetic: VMTs ---------------------------------------------------------


def test_scrape_vmts_synthetic():
    image, vmt_a, vmt_b = vmt_image()
    vmts = S.scrape_vmts(image, ("TFruityPlug", "TFruityPlugHost"))
    assert vmts["TFruityPlug"].address == vmt_a
    assert vmts["TFruityPlug"].instance_size == 0xA4
    assert vmts["TFruityPlugHost"].address == vmt_b
    assert vmts["TFruityPlugHost"].instance_size == 0x100


def test_first_method_offset_synthetic():
    image, vmt_a, vmt_b = vmt_image(first_diff_offset=200)
    assert S.first_method_offset(image, vmt_a, vmt_b) == 200


def test_first_method_offset_tracks_header_size():
    """A future FPC with a bigger VMT header moves the answer, not breaks it."""
    image, vmt_a, vmt_b = vmt_image(first_diff_offset=232)
    assert S.first_method_offset(image, vmt_a, vmt_b) == 232


def test_missing_vmt_raises():
    with pytest.raises(S.ScrapeError, match="TFruityHost"):
        S.scrape_vmts(vmt_image()[0], ("TFruityHost",))


# --- synthetic: dispatcher guard --------------------------------------------


def test_detect_dispatcher_bound():
    assert S.detect_dispatcher_bound(guard_code(109), 0x100) == 109
    assert S.detect_dispatcher_bound(guard_code(150), 0x100) == 150


def test_detect_jump_table():
    # adrp at page of its own pc (immhi=immlo=0) + add #0xa00
    code_at = 0x100
    image = guard_code(109, at=code_at)
    adrp_pc = code_at + 4 * 14
    expected = ((adrp_pc >> 12) << 12) + 0xA00
    assert S.detect_jump_table(image, code_at) == expected


def test_unrecognized_prologue_returns_none():
    nops = b"".join(struct.pack("<I", 0xD503201F) for _ in range(64))
    assert S.detect_dispatcher_bound(nops, 0) is None
    # cmp without b.hi
    partial = nops + struct.pack("<II", 0xF101B41F, 0xD503201F) + nops
    assert S.detect_dispatcher_bound(partial, 0) is None
    # cmp + b.hi but no jump-table br after it
    no_table = nops + struct.pack("<II", 0xF101B41F, 0x54000048) + b"\0" * 64
    assert S.detect_dispatcher_bound(no_table, 0) is None


# --- slice selection ---------------------------------------------------------


def _thin_arm64(payload: bytes) -> bytes:
    return struct.pack("<II", 0xFEEDFACF, 0x0100000C) + payload


def test_arm64_slice_thin_and_fat(tmp_path):
    thin = _thin_arm64(b"PAYLOAD")
    fat = struct.pack(">II", 0xCAFEBABE, 2)
    fat += struct.pack(">IIIII", 0x01000007, 3, 0x100, 4, 0)  # x86_64 slice
    fat += struct.pack(">IIIII", 0x0100000C, 0, 0x200, len(thin), 0)
    fat = fat.ljust(0x200, b"\0") + thin

    thin_path = tmp_path / "thin.dylib"
    thin_path.write_bytes(thin)
    assert S.arm64_slice(thin_path) == thin
    fat_path = tmp_path / "fat.dylib"
    fat_path.write_bytes(fat)
    assert S.arm64_slice(fat_path) == thin


def test_arm64_slice_rejects_non_macho(tmp_path):
    path = tmp_path / "noise.bin"
    path.write_bytes(b"this is not a mach-o image")
    with pytest.raises(S.ScrapeError):
        S.arm64_slice(path)


# --- merge: scraper-owned blocks only ----------------------------------------


def _fake_result(bound: int | None = 109, table: int | None = 0x42DA00) -> S.ScrapeResult:
    return S.ScrapeResult(
        dylib_sha256="cafe" * 16,
        structs={
            "TNoteParams": StructLayout(size=32, fields=(StructField("Vol", 12, "Single"),))
        },
        vmts={"TFruityHost": S.VmtFacts(address=0x13FA618, instance_size=0x120, parent_addr=0)},
        vmt_first_method_offset=200,
        host_dispatcher_addr=0x427BC0,
        handled_max_id=bound,
        jump_table_addr=table,
    )


def test_merge_updates_scraper_blocks_only():
    doc = packaged_profiles()[PACKAGED_NAME]
    before = json.dumps(doc, sort_keys=True)
    merged = S.merge_scrape(doc, _fake_result(), "2026-08-31T00:00:00Z")
    assert json.dumps(doc, sort_keys=True) == before  # input never mutated
    assert merged["meta"]["engine_dylib_sha256"] == "cafe" * 16
    assert merged["meta"]["generated"] == "2026-08-31T00:00:00Z"
    assert "rtti" in merged["meta"]["sources"] and "vmt" in merged["meta"]["sources"]
    assert list(merged["structs"]) == ["TNoteParams"]
    # live-probe-owned blocks are preserved verbatim
    for block in ("getters", "push_events", "tick", "capabilities", "flp", "fst", "spool_roots", "ppq"):
        assert merged[block] == doc[block]
    assert merged["dispatcher"]["unsafe"] == doc["dispatcher"]["unsafe"]
    assert merged["dispatcher"]["inert_in_range"] == doc["dispatcher"]["inert_in_range"]


def test_merge_keeps_bound_when_undetected():
    doc = packaged_profiles()[PACKAGED_NAME]
    merged = S.merge_scrape(doc, _fake_result(bound=None, table=None), "2026-08-31T00:00:00Z")
    assert merged["dispatcher"]["handled_max_id"] == doc["dispatcher"]["handled_max_id"]
    assert merged["dispatcher"]["jump_table_addr"] == doc["dispatcher"]["jump_table_addr"]


# --- golden: the installed FL reproduces the packaged profile ----------------


@pytest.fixture(scope="module")
def golden_scrape() -> S.ScrapeResult:
    if not FL_DYLIB.is_file():
        pytest.skip("no FL 2026 install to scrape")
    return S.scrape(FL_DYLIB)


@pytest.fixture(scope="module")
def packaged():
    return parse_profile(packaged_profiles()[PACKAGED_NAME])


@needs_fl
def test_golden_structs_reproduce_packaged(golden_scrape, packaged):
    """THE drift alarm: scraping the installed engine must reproduce the
    packaged structs block exactly (would have caught PR #62's Vol-is-Single)."""
    assert golden_scrape.structs == packaged.structs


@needs_fl
def test_golden_dispatcher_facts(golden_scrape, packaged):
    assert golden_scrape.vmt_first_method_offset == packaged.dispatcher.vmt_first_method_offset
    assert golden_scrape.host_dispatcher_addr == packaged.dispatcher.host_dispatcher_addr
    assert golden_scrape.handled_max_id == packaged.dispatcher.handled_max_id
    assert golden_scrape.jump_table_addr == packaged.dispatcher.jump_table_addr


@needs_fl
def test_golden_engine_hash(golden_scrape, packaged):
    """A different dylib hash with green struct tests is fine; a matching hash
    with different facts is impossible. This pins which engine the packaged
    profile describes."""
    assert golden_scrape.dylib_sha256 == packaged.meta.engine_dylib_sha256


@needs_fl
def test_golden_vmt_instance_sizes(golden_scrape):
    assert golden_scrape.vmts["TFruityPlug"].instance_size == 0xA4
    assert golden_scrape.vmts["TFruityPlugHost"].instance_size == 0x100
    assert golden_scrape.vmts["TFruityHost"].instance_size == 0x120
