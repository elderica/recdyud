cmake_minimum_required(VERSION 3.20)

# Native helpers for recdyud.  Built by scikit-build-core during `uv sync`.
#
#   * libusb-1.0  (vendor/libusb-cmake)  - backend for PyUSB
#   * libdyudb25  (vendor/libaribb25 + native/dyud_b25.c) - ARIB STD-B25 descrambler
#
# Both are built from the vendored sources; no system-wide C libraries are used.

project(recdyud_native LANGUAGES C CXX)

set(RECDYUD_NATIVE_INSTALL_DIR "recdyud/_native" CACHE STRING "Install directory of native libraries (relative to the wheel root)")

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(VENDOR_DIR "${CMAKE_CURRENT_SOURCE_DIR}/vendor")
foreach(_probe libaribb25/aribb25/arib_std_b25.c libusb-cmake/CMakeLists.txt)
  if(NOT EXISTS "${VENDOR_DIR}/${_probe}")
    message(FATAL_ERROR "vendor/${_probe} is missing. Run: git submodule update --init --recursive")
  endif()
endforeach()

# ---------------------------------------------------------------------------
# libusb-1.0
# ---------------------------------------------------------------------------

set(LIBUSB_BUILD_SHARED_LIBS ON CACHE BOOL "" FORCE)
set(LIBUSB_BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(LIBUSB_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LIBUSB_INSTALL_TARGETS OFF CACHE BOOL "" FORCE)
# Use the netlink backend so that libudev is not required (also works in containers).
set(LIBUSB_ENABLE_UDEV OFF CACHE BOOL "" FORCE)
add_subdirectory("${VENDOR_DIR}/libusb-cmake" libusb)

install(TARGETS usb-1.0
  LIBRARY DESTINATION "${RECDYUD_NATIVE_INSTALL_DIR}"
  RUNTIME DESTINATION "${RECDYUD_NATIVE_INSTALL_DIR}")

# ---------------------------------------------------------------------------
# libdyudb25
# ---------------------------------------------------------------------------

set(ARIBB25_DIR "${VENDOR_DIR}/libaribb25/aribb25")

# Only the descrambler core is compiled; b_cas_card.c (PC/SC) is replaced by
# native/dyud_b25.c, which forwards card access to recdyud.
add_library(dyudb25 SHARED
  native/dyud_b25.c
  "${ARIBB25_DIR}/arib_std_b25.c"
  "${ARIBB25_DIR}/multi2.cc"
  "${ARIBB25_DIR}/ts_section_parser.c")
target_include_directories(dyudb25 PRIVATE "${ARIBB25_DIR}")
target_compile_definitions(dyudb25 PRIVATE _LARGEFILE_SOURCE _FILE_OFFSET_BITS=64)
set_target_properties(dyudb25 PROPERTIES
  C_STANDARD 11
  CXX_STANDARD 17
  POSITION_INDEPENDENT_CODE ON
  C_VISIBILITY_PRESET hidden
  CXX_VISIBILITY_PRESET hidden
  PREFIX "lib")
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(dyudb25 PRIVATE -O3 -w)
  if(NOT APPLE)
    # Keep the C++ runtime private so the library works in slim images too.
    target_link_options(dyudb25 PRIVATE -static-libstdc++ -static-libgcc -Wl,--exclude-libs,ALL)
  endif()
endif()

install(TARGETS dyudb25
  LIBRARY DESTINATION "${RECDYUD_NATIVE_INSTALL_DIR}"
  RUNTIME DESTINATION "${RECDYUD_NATIVE_INSTALL_DIR}")
