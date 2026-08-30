cmake_minimum_required(VERSION 3.20)
project(fl_mcp_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The FL Studio SDK is proprietary — its headers carry an Image-Line copyright
# with no redistribution grant, so it is NOT vendored in this repo. Fetch it
# with scripts/fetch_sdk.sh, or point FL_SDK_DIR at your own copy.
set(FL_SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/sdk" CACHE PATH "FL Studio SDK directory")

# The FL 2026 host call contract (native/abi/fl_host_abi.h) and the FPC-shaped
# mock host that proves it offline. Shared by the Bridge, the Probe, and every
# test: one contract, one place, no per-target copy to drift.
set(FL_ABI_DIR "${CMAKE_CURRENT_SOURCE_DIR}/abi")

if(NOT EXISTS "${FL_SDK_DIR}/fp_plugclass.h")
    message(FATAL_ERROR
        "FL Studio SDK not found at ${FL_SDK_DIR}\n"
        "Run: native/scripts/fetch_sdk.sh\n"
        "Or:  cmake -DFL_SDK_DIR=/path/to/sdk ..")
endif()

# FL's native plugins on macOS are universal binaries; match so the same build
# works on Intel and Apple Silicon.
if(APPLE)
    set(CMAKE_OSX_ARCHITECTURES "arm64;x86_64" CACHE STRING "" FORCE)
endif()

add_library(fl_sdk STATIC
    "${FL_SDK_DIR}/fp_plugclass.cpp"
    "${FL_SDK_DIR}/fp_cplug.cpp"
    "${FL_SDK_DIR}/fp_extra.cpp"
)
target_include_directories(fl_sdk PUBLIC "${FL_SDK_DIR}")

# Hide SDK symbols. Without this the plugin exports TFruityPlug's vtables and
# every TCPPFruityPlug method, which could collide with other FL plugins loaded
# into the same process. Image-Line's own effects export exactly two symbols;
# match that surface.
set_target_properties(fl_sdk PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN ON
)

# The SDK predates 64-bit macOS: __stdcall is meaningless here and NULL is
# redefined. Both are harmless but noisy, and they are Image-Line's code to fix,
# not ours — silence them so real warnings in our own code stay visible.
target_compile_options(fl_sdk PUBLIC
    -Wno-ignored-attributes
    -Wno-macro-redefined
    -Wno-deprecated-declarations
)


# --- FL MCP Bridge: the production note-writing plugin ------------------------
# Base-free, 64-slot ABI (the proven outcome of the Spike 1 crash saga; see
# native/bridge/BRIDGE_FINDINGS.md), with the note-write + spool-consume logic
# factored into bridge_core.* for unit testing under ASan/UBSan. Self-contained:
# it reimplements the TFruityPlug ABI directly and calls no SDK-cpp symbol, so
# it links no fl_sdk lib at all (only the header, for struct layouts and the
# static_assert contract).
add_library(bridge SHARED
    bridge/src/bridge.cpp
    bridge/src/bridge_core.cpp
    bridge/src/bridge_gui.cpp
    bridge/src/bridge_udp.cpp
)
target_include_directories(bridge PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" bridge/src)
target_compile_options(bridge PRIVATE
    -Wno-ignored-attributes -Wno-macro-redefined -Wno-deprecated-declarations
)
set_target_properties(bridge PROPERTIES
    OUTPUT_NAME "FL MCP Bridge_x64"
    PREFIX ""
    SUFFIX ".dylib"
    CXX_VISIBILITY_PRESET hidden
)

# Bridge core unit test: drives PollOnce/BuildNotesParams against a mock host
# with a temp spool dir, no FL needed. ASan+UBSan.
add_executable(bridge_core_test
    bridge/tests/bridge_core_test.cpp
    bridge/src/bridge_core.cpp
)
target_include_directories(bridge_core_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" bridge/src)
target_compile_options(bridge_core_test PRIVATE
    -fsanitize=address,undefined -g
    -Wno-ignored-attributes
)
target_link_options(bridge_core_test PRIVATE -fsanitize=address,undefined)

# GUI-thread marshal test: the property the FHD_AddNotesToPR deadlock fix rests
# on - work handed over from a worker thread runs on the MAIN thread, and only
# once the main thread's run loop gets a turn. Links CoreFoundation on Apple so
# the test can drain the main queue the same way FL's run loop does.
add_executable(bridge_gui_test
    bridge/tests/bridge_gui_test.cpp
    bridge/src/bridge_gui.cpp
)
target_include_directories(bridge_gui_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" bridge/src)
target_compile_options(bridge_gui_test PRIVATE
    -fsanitize=address,undefined -g
    -Wno-ignored-attributes
)
target_link_options(bridge_gui_test PRIVATE -fsanitize=address,undefined)
if(APPLE)
    target_link_libraries(bridge_gui_test PRIVATE "-framework CoreFoundation")
endif()

# UDP transport test: queue discipline, executor equivalence across carriers,
# datagram auth/parse failures, real-socket round trip, bounded shutdown.
# ASan+UBSan like bridge_core_test.
add_executable(bridge_udp_test
    bridge/tests/bridge_udp_test.cpp
    bridge/src/bridge_core.cpp
    bridge/src/bridge_udp.cpp
)
target_include_directories(bridge_udp_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" bridge/src)
target_compile_options(bridge_udp_test PRIVATE
    -fsanitize=address,undefined -g
    -Wno-ignored-attributes
)
target_link_options(bridge_udp_test PRIVATE -fsanitize=address,undefined)

# --- FL MCP Probe: a throwaway diagnostic plugin (NOT production) -------------
# Answers three live questions (note readback? project PPQ query? which FHD_*
# getters cover the controller read surface?) then is deleted once its verdict
# lands - the same lifecycle as the v1 Spike 1.5 probe. It reproduces the
# Bridge's proven base-free 64-slot ABI shape (pinned by the same layout
# static_asserts plus the vtable-slot check below) but forks none of the
# Bridge's logic. All probe logic is in probe_core.* and unit-tested under
# ASan/UBSan against a mock host (native/probe/tests/probe_core_test.cpp).
add_library(probe SHARED
    probe/src/probe.cpp
    probe/src/probe_core.cpp
    probe/src/probe_io.cpp
)
target_include_directories(probe PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" probe/src)
target_compile_options(probe PRIVATE
    -Wno-ignored-attributes -Wno-macro-redefined -Wno-deprecated-declarations
)
set_target_properties(probe PROPERTIES
    OUTPUT_NAME "FL MCP Probe_x64"
    PREFIX ""
    SUFFIX ".dylib"
    CXX_VISIBILITY_PRESET hidden
)

# Probe core unit test: drives the getters/sweep/deepprobe/validate passes,
# checkpoint/resume, static-ledger skip, and crash quarantine against a mock
# host with a temp spool dir, no FL needed. ASan+UBSan.
add_executable(probe_core_test
    probe/tests/probe_core_test.cpp
    probe/src/probe_core.cpp
    probe/src/probe_io.cpp
)
target_include_directories(probe_core_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" probe/src)
target_compile_options(probe_core_test PRIVATE
    -fsanitize=address,undefined -g
    -Wno-ignored-attributes
)
target_link_options(probe_core_test PRIVATE -fsanitize=address,undefined)

enable_testing()
add_test(NAME bridge_core_test COMMAND bridge_core_test)
add_test(NAME bridge_gui_test COMMAND bridge_gui_test)
add_test(NAME bridge_udp_test COMMAND bridge_udp_test)
add_test(NAME probe_core_test COMMAND probe_core_test)

# ABI invariant check: the load-bearing property the data static_asserts do NOT
# cover is the vtable slot count. Assert each plugin dylib has exactly 64 vtable
# slots (base-free, no virtual destructor) - the technique BRIDGE_FINDINGS.md
# documents. macOS/Mach-O only; the script no-ops (passes) elsewhere so ctest
# stays green cross-platform.
if(APPLE)
    add_test(NAME bridge_abi_check
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check_vtable.sh"
                "$<TARGET_FILE:bridge>" Bridge)
    add_test(NAME probe_abi_check
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check_vtable.sh"
                "$<TARGET_FILE:probe>" Probe)
endif()

# Differential-test shim: same PollOnce + mock host, driven by the Python suite
# (mcp-server/tests/test_bridge_differential.py) over a spool written by the
# real v2 Spool class. Plain build (no sanitizers) so pytest can spawn it
# without sanitizer runtime concerns; the sanitized coverage of the same code
# is bridge_core_test.
add_executable(bridge_spool_cli
    bridge/tools/bridge_spool_cli.cpp
    bridge/src/bridge_core.cpp
)
target_include_directories(bridge_spool_cli PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" bridge/src bridge/tests)
target_compile_options(bridge_spool_cli PRIVATE
    -Wno-ignored-attributes -Wno-macro-redefined -Wno-deprecated-declarations
)
