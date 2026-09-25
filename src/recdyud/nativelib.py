"""Locate the native libraries built from the vendored sources."""

import os
from pathlib import Path

import recdyud

LIBUSB = "libusb-1.0.so"
LIBDYUDB25 = "libdyudb25.so"


class NativeLibraryNotFound(RuntimeError):
    pass


def find_library(name: str) -> str:
    """Return the path of a bundled native library.

    ``RECDYUD_NATIVE_DIR`` overrides the search path (useful for development
    builds made with plain CMake).
    """
    candidates: list[Path] = []
    if override := os.environ.get("RECDYUD_NATIVE_DIR"):
        candidates.append(Path(override) / name)
    # In an editable install the package spans the source tree and site-packages.
    for entry in recdyud.__path__:
        candidates.append(Path(entry) / "_native" / name)
    for path in candidates:
        if path.is_file():
            return str(path)
    raise NativeLibraryNotFound(
        f"{name} not found (searched: {', '.join(map(str, candidates))}). "
        "Reinstall recdyud with `uv sync` after `git submodule update --init --recursive`."
    )
