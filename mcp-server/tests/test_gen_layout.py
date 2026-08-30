"""Codegen: the generated native header is deterministic and pins the layout.

The checked-in native/generated/fl_profile_generated.h must regenerate
byte-for-byte from the packaged profile (so CMake re-running the generator
never churns the tree), and the emitted static_assert wall must carry every
version-specific fact the hand-maintained block did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fl_mcp import gen_layout
from fl_mcp.gen_layout import GenError, generate
from fl_mcp.profile import packaged_profiles

PACKAGED_NAME = "fl-26.1.5-5384-arm64.json"
GENERATED_HEADER = Path(__file__).resolve().parents[2] / "native" / "generated" / "fl_profile_generated.h"


@pytest.fixture
def doc() -> dict:
    return packaged_profiles()[PACKAGED_NAME]


def test_checked_in_header_is_up_to_date(doc):
    """The committed header equals a fresh generation - CMake regeneration is
    a no-op, and a stale header is a test failure, not a silent drift."""
    assert GENERATED_HEADER.read_text() == generate(doc)


def test_generation_is_deterministic(doc):
    assert generate(doc) == generate(doc)


def test_wall_pins_vol_as_single(doc):
    text = generate(doc)
    assert "static_assert(offsetof(TNoteParams, Vol) == 12" in text
    assert "TNoteParams.Vol is 4 bytes (Single in FL's RTTI)" in text


def test_carries_all_migrated_constants(doc):
    text = generate(doc)
    assert "kHandledMaxId = 109" in text
    assert "kDefaultPPQ = 96" in text
    assert "kVmtFirstMethodOffset = 200" in text
    assert "kInertInRange[] = {39, 62, 63, 64, 102}" in text
    # the unsafe ledger, from the profile not from C++ (L4's pinned shape)
    assert "kUnsafeIds[] = {94, 99}" in text
    assert "kUnsafeIdCount = 2" in text
    # size asserts for both records the native ABI hands FL
    assert 'static_assert(sizeof(TNoteParams) == 32' in text
    assert 'static_assert(sizeof(TNotesParams) == 52' in text


def test_l4_consumption_contract(doc):
    """The exact surface L4's bridge_core consumes behind __has_include
    (pinned 2026-08-30): the detect macro, the guard macro around the assert
    wall, and NO SDK include from this header itself."""
    text = generate(doc)
    assert "#define FLMCP_PROFILE_GENERATED 1" in text
    assert "#ifndef FLMCP_PROFILE_GENERATED_H" in text
    assert "#ifdef FLMCP_ASSERT_STRUCTS" in text
    assert "#endif  // FLMCP_ASSERT_STRUCTS" in text
    assert '#include "fp_plugclass.h"' not in text
    # the wall sits between the guard pair, constants outside it
    guard = text.index("#ifdef FLMCP_ASSERT_STRUCTS")
    assert text.index("kHandledMaxId") < guard < text.index("static_assert(sizeof(TNoteParams)")


def test_provenance_stamped(doc):
    text = generate(doc)
    assert "FL 26.1.5 build 5384 arm64" in text
    assert doc["meta"]["engine_dylib_sha256"] in text
    assert "DO NOT EDIT" in text


def test_empty_unsafe_ledger_refused(doc):
    broken = {**doc, "dispatcher": {**doc["dispatcher"], "unsafe": []}}
    with pytest.raises(GenError, match="unlearns 94/99"):
        generate(broken)


def test_missing_asserted_struct_refused(doc):
    structs = {k: v for k, v in doc["structs"].items() if k != "TNotesParams"}
    with pytest.raises(GenError, match="TNotesParams"):
        generate({**doc, "structs": structs})


def test_cli_writes_output(tmp_path, doc):
    import json

    profile_path = tmp_path / "p.json"
    profile_path.write_text(json.dumps(doc))
    out = tmp_path / "sub" / "out.h"
    assert gen_layout.main([str(profile_path), "-o", str(out)]) == 0
    assert out.read_text() == generate(doc)


# --- compile gate: the header behaves exactly as L4's TU consumes it ---------

SDK_HEADER = Path(__file__).resolve().parents[2] / "native" / "sdk" / "fp_plugclass.h"


def _compiles(tmp_path: Path, header_text: str, tu_text: str, *extra_args: str) -> bool:
    import shutil
    import subprocess

    (tmp_path / "fl_profile_generated.h").write_text(header_text)
    tu = tmp_path / "tu.cpp"
    tu.write_text(tu_text)
    clang = shutil.which("clang++")
    result = subprocess.run(
        [clang, "-std=c++17", "-fsyntax-only", f"-I{tmp_path}", *extra_args, str(tu)],
        capture_output=True,
        timeout=120,
    )
    return result.returncode == 0


needs_toolchain = pytest.mark.skipif(
    __import__("shutil").which("clang++") is None or not SDK_HEADER.is_file(),
    reason="needs clang++ and the fetched SDK (native/scripts/fetch_sdk.sh)",
)

# Exactly L4's consumption shape: SDK first, then the guard macro, then us.
ASSERTING_TU = """
#include "fp_plugclass.h"
#define FLMCP_ASSERT_STRUCTS
#include "fl_profile_generated.h"
int main() { return fl_profile::kHandledMaxId; }
"""


def test_header_is_sdk_free_standalone(tmp_path, doc):
    """Without FLMCP_ASSERT_STRUCTS the header must compile with NO SDK on the
    include path at all - that is what makes it safe under __has_include."""
    import shutil

    if shutil.which("clang++") is None:
        pytest.skip("needs clang++")
    tu = '#include "fl_profile_generated.h"\nint main() { return fl_profile::kUnsafeIds[0]; }\n'
    assert _compiles(tmp_path, generate(doc), tu)


@needs_toolchain
def test_wall_compiles_against_sdk(tmp_path, doc):
    assert _compiles(
        tmp_path, generate(doc), ASSERTING_TU, f"-I{SDK_HEADER.parent}",
        "-Wno-ignored-attributes", "-Wno-macro-redefined", "-Wno-deprecated-declarations",
    )


@needs_toolchain
def test_wall_refuses_shifted_vol(tmp_path, doc):
    """The differential: a profile whose Vol moved compile-FAILS against the
    real SDK header - a layout shift can never build, let alone reach FL."""
    import json

    shifted = json.loads(json.dumps(doc))
    for field in shifted["structs"]["TNoteParams"]["fields"]:
        if field["name"] == "Vol":
            field["offset"] = 16
    assert not _compiles(
        tmp_path, generate(shifted), ASSERTING_TU, f"-I{SDK_HEADER.parent}",
        "-Wno-ignored-attributes", "-Wno-macro-redefined", "-Wno-deprecated-declarations",
    )
