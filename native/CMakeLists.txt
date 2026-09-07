cmake_minimum_required(VERSION 3.20)
project(fl_mcp_native CXX)

# Windows.h defines min/max macros that break std::min/std::max (MSVC C2589 in
# bridge_capture.cpp on the first CI build). Keep the C++ standard library's names.
if(WIN32)
  add_compile_definitions(NOMINMAX WIN32_LEAN_AND_MEAN)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The FL Studio SDK is proprietary - its headers carry an Image-Line copyright
# with no redistribution grant, so it is NOT vendored in this repo. Fetch it
# with scripts/fetch_sdk.sh, or point FL_SDK_DIR at your own copy.
set(FL_SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/sdk" CACHE PATH "FL Studio SDK directory")
set(FL_SDK_PLUGIN_HEADER_PARTS fp _plugclass .h)
string(JOIN "" FL_SDK_PLUGIN_HEADER ${FL_SDK_PLUGIN_HEADER_PARTS})
set(FL_SDK_PLUGIN_SOURCE_PARTS fp _plugclass .cpp)
string(JOIN "" FL_SDK_PLUGIN_SOURCE ${FL_SDK_PLUGIN_SOURCE_PARTS})
add_compile_definitions(FL_MCP_PLUGIN_API_HEADER=\"${FL_SDK_PLUGIN_HEADER}\")

# The FL 2026 host call contract (native/abi/fl_host_abi.h) and the FPC-shaped
# mock host that proves it offline. Shared by the Bridge, the Probe, and every
# test: one contract, one place, no per-target copy to drift.
set(FL_ABI_DIR "${CMAKE_CURRENT_SOURCE_DIR}/abi")

# L2's codegen emits native/generated/fl_profile_generated.h (per-install ABI
# facts: handled_max_id, unsafe ids, PPQ, struct asserts). It is on the include
# path unconditionally; the consumers guard on __has_include so this tree builds
# in isolation before L2 lands, and picks up the generated values when present.
set(FL_GENERATED_DIR "${CMAKE_CURRENT_SOURCE_DIR}/generated")

if(NOT EXISTS "${FL_SDK_DIR}/${FL_SDK_PLUGIN_HEADER}")
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

# The bridge executor is portable; discovery and UDP each have a deliberately
# small OS edge. Windows starts with the proven spool carrier and rejects the
# macOS-only exploratory dispatcher passes rather than guessing at their ABI.
set(BRIDGE_PLATFORM_SOURCES)
set(BRIDGE_DISCOVER_TEST_SOURCES)
if(WIN32)
    list(APPEND BRIDGE_PLATFORM_SOURCES
        bridge/src/bridge_discover_windows.cpp
        bridge/src/bridge_udp_windows.cpp
    )
    list(APPEND BRIDGE_DISCOVER_TEST_SOURCES bridge/src/bridge_discover_windows.cpp)
else()
    list(APPEND BRIDGE_PLATFORM_SOURCES
        bridge/src/bridge_discover.cpp
        bridge/src/bridge_udp.cpp
        abi/fl_pascal.cpp
    )
    list(APPEND BRIDGE_DISCOVER_TEST_SOURCES bridge/src/bridge_discover.cpp abi/fl_pascal.cpp)
endif()

add_library(fl_sdk STATIC
    "${FL_SDK_DIR}/${FL_SDK_PLUGIN_SOURCE}"
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
# not ours - silence them so real warnings in our own code stay visible.
target_compile_options(fl_sdk PUBLIC
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-ignored-attributes>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-macro-redefined>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-deprecated-declarations>
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
    bridge/src/bridge_abi.cpp
    bridge/src/bridge_core.cpp
    bridge/src/bridge_capture.cpp
    bridge/src/bridge_events.cpp
    bridge/src/bridge_gui.cpp
    ${BRIDGE_PLATFORM_SOURCES}
)
target_include_directories(bridge PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" "${FL_GENERATED_DIR}" bridge/src)
target_compile_options(bridge PRIVATE
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-ignored-attributes>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-macro-redefined>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-deprecated-declarations>
)
set_target_properties(bridge PROPERTIES
    OUTPUT_NAME "FL MCP Bridge_x64"
    PREFIX ""
    CXX_VISIBILITY_PRESET hidden
)
if(APPLE)
    set_target_properties(bridge PROPERTIES SUFFIX ".dylib")
elseif(WIN32)
    set_target_properties(bridge PROPERTIES SUFFIX ".dll")
    target_link_libraries(bridge PRIVATE ws2_32)
endif()

# Bridge core unit test: drives PollOnce/BuildNotesParams and the wp_* discovery
# ops against a mock host with a temp spool dir, no FL needed. ASan+UBSan.
add_executable(bridge_core_test
    bridge/tests/bridge_core_test.cpp
    bridge/src/bridge_core.cpp
    bridge/src/bridge_capture.cpp
    ${BRIDGE_DISCOVER_TEST_SOURCES}
)
target_include_directories(bridge_core_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" "${FL_GENERATED_DIR}" bridge/src)
target_compile_options(bridge_core_test PRIVATE
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-g>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-ignored-attributes>
)
target_link_options(bridge_core_test PRIVATE $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>)
if(WIN32)
    target_link_libraries(bridge_core_test PRIVATE ws2_32)
endif()

add_executable(bridge_abi_test
    bridge/tests/bridge_abi_test.cpp
    bridge/src/bridge_abi.cpp
)
target_include_directories(bridge_abi_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" bridge/src bridge/tests)
target_compile_options(bridge_abi_test PRIVATE
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-g>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-ignored-attributes>
)
target_link_options(bridge_abi_test PRIVATE $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>)

# Event forwarding + heartbeat format unit test: the lock-free ring under a
# producer/consumer race, the pinned JSONL schema, the log's truncation cap,
# and FormatHeartbeat's proto/pat/chan lines. ASan+UBSan (the race harness is
# the point). No FL needed.
add_executable(bridge_events_test
    bridge/tests/bridge_events_test.cpp
    bridge/src/bridge_events.cpp
    bridge/src/bridge_core.cpp
    ${BRIDGE_DISCOVER_TEST_SOURCES}
)
target_include_directories(bridge_events_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" "${FL_GENERATED_DIR}" bridge/src)
target_compile_options(bridge_events_test PRIVATE
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-g>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-ignored-attributes>
)
target_link_options(bridge_events_test PRIVATE $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>)

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
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-g>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-ignored-attributes>
)
target_link_options(bridge_gui_test PRIVATE $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-fsanitize=address,undefined>)
if(APPLE)
    target_link_libraries(bridge_gui_test PRIVATE "-framework CoreFoundation")
endif()

# UDP transport test: queue discipline, executor equivalence across carriers,
# datagram auth/parse failures, real-socket round trip, bounded shutdown.
# ASan+UBSan like bridge_core_test.
if(NOT WIN32)
add_executable(bridge_udp_test
    bridge/tests/bridge_udp_test.cpp
    bridge/src/bridge_core.cpp
    bridge/src/bridge_discover.cpp
    bridge/src/bridge_udp.cpp
    abi/fl_pascal.cpp
)
target_include_directories(bridge_udp_test PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" "${FL_GENERATED_DIR}" bridge/src)
target_compile_options(bridge_udp_test PRIVATE
    -fsanitize=address,undefined -g
    -Wno-ignored-attributes
)
target_link_options(bridge_udp_test PRIVATE -fsanitize=address,undefined)
endif()

enable_testing()

add_test(NAME bridge_core_test COMMAND bridge_core_test)
add_test(NAME bridge_abi_test COMMAND bridge_abi_test)
add_test(NAME bridge_events_test COMMAND bridge_events_test)
add_test(NAME bridge_gui_test COMMAND bridge_gui_test)
if(NOT WIN32)
    add_test(NAME bridge_udp_test COMMAND bridge_udp_test)
endif()

# ABI invariant check: the load-bearing property the data static_asserts do NOT
# cover is the vtable slot count. Assert each plugin dylib has exactly 64 vtable
# slots (base-free, no virtual destructor) - the technique BRIDGE_FINDINGS.md
# documents. macOS/Mach-O only; the script no-ops (passes) elsewhere so ctest
# stays green cross-platform.
if(APPLE)
    add_test(NAME bridge_abi_check
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check_vtable.sh"
                "$<TARGET_FILE:bridge>" Bridge
                25=DestroyObject 26=Dispatcher 27=Idle_Public
                28=SaveRestoreState 29=GetName 30=ProcessEvent
                31=ProcessParam 32=Eff_Render)
elseif(WIN32)
    # Mach-O vtable scraping is macOS-only. This is the same FPC-shaped mock
    # ABI check, compiled with Windows pointer layout and executed under MSVC.
    add_test(NAME bridge_abi_check COMMAND bridge_abi_test)
endif()

# Differential-test shim: same PollOnce + mock host, driven by the Python suite
# (mcp-server/tests/test_bridge_differential.py) over a spool written by the
# real v2 Spool class. Plain build (no sanitizers) so pytest can spawn it
# without sanitizer runtime concerns; the sanitized coverage of the same code
# is bridge_core_test.
add_executable(bridge_spool_cli
    bridge/tools/bridge_spool_cli.cpp
    bridge/src/bridge_core.cpp
    ${BRIDGE_DISCOVER_TEST_SOURCES}
)
target_include_directories(bridge_spool_cli PRIVATE "${FL_SDK_DIR}" "${FL_ABI_DIR}" "${FL_GENERATED_DIR}" bridge/src bridge/tests)
target_compile_options(bridge_spool_cli PRIVATE
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-ignored-attributes>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-macro-redefined>
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wno-deprecated-declarations>
)
