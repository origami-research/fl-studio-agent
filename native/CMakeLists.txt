cmake_minimum_required(VERSION 3.20)
project(fl_mcp_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The FL Studio SDK is proprietary — its headers carry an Image-Line copyright
# with no redistribution grant, so it is NOT vendored in this repo. Fetch it
# with scripts/fetch_sdk.sh, or point FL_SDK_DIR at your own copy.
set(FL_SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/sdk" CACHE PATH "FL Studio SDK directory")

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

# --- Spike 1 -----------------------------------------------------------------
# Answers whether FHD_AddNotesToPR can target an arbitrary channel from an
# effect-registered plugin. See native/spike1/README.md.
add_library(spike1 SHARED spike1/src/spike1.cpp)
target_link_libraries(spike1 PRIVATE fl_sdk)

# FL discovers native plugins as <Name>/<Name>_x64.dylib — the suffix is part of
# the contract even on arm64, matching Image-Line's own bundled effects.
set_target_properties(spike1 PROPERTIES
    OUTPUT_NAME "FL MCP Spike1_x64"
    PREFIX ""
    SUFFIX ".dylib"
    CXX_VISIBILITY_PRESET hidden
)

# --- FL MCP Bridge -----------------------------------------------------------
# The real thing: reads the same spool the MCP server writes, so note edits
# apply with no user action. Replaces the piano roll .pyscript's manual trigger.
add_library(bridge SHARED bridge/src/bridge.cpp)
target_link_libraries(bridge PRIVATE fl_sdk)
target_include_directories(bridge PRIVATE bridge/src)
set_target_properties(bridge PROPERTIES
    OUTPUT_NAME "FL MCP Bridge_x64"
    PREFIX ""
    SUFFIX ".dylib"
    CXX_VISIBILITY_PRESET hidden
)
