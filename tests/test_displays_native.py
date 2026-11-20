"""Displays' Swift (Native/Displays): its pure parts in tests/displays/main.swift (the scale, the nits,
the gamma factor, the steps, the HUD's segments, DDC packets, matching channels to displays, key events,
the sound output), and the dylib the build makes, loaded the way displays.py loads it. Nothing here
starts it: no display is touched, no window is shown and no tap is made. Skipped without a Swift
compiler."""

import ctypes
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(str(p) for p in (ROOT / "Native/Displays").glob("*.swift"))

needs_swift = pytest.mark.skipif(
    not shutil.which("xcrun") or subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True).returncode,
    reason="no Swift compiler")


@needs_swift
def test_pure_parts(tmp_path):
    exe = tmp_path / "displays-tests"
    subprocess.run(["xcrun", "swiftc", *SOURCES, str(ROOT / "tests/displays/main.swift"), "-o", str(exe)],
                   check=True, capture_output=True, timeout=300)
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_swift
def test_dylib_builds_and_has_its_functions(tmp_path):
    """Built as scripts/build.sh builds it, and wrapped by displays.Library like the app, without starting it."""
    import displays

    lib = tmp_path / "libdisplays.dylib"
    subprocess.run(["xcrun", "swiftc", "-O", "-target", "arm64-apple-macos13.0", "-emit-library", "-module-name", "Displays",
                    *SOURCES, "-o", str(lib)], check=True, capture_output=True, timeout=300)
    library = displays.Library(lib)
    assert not library.keys_running()
    raw = ctypes.CDLL(str(lib))
    for name in ("displays_start", "displays_list", "displays_free", "displays_refresh", "displays_set_brightness",
                 "displays_set_volume", "displays_set_keys", "displays_stop_keys", "displays_keys_running"):
        assert isinstance(getattr(raw, name), ctypes._CFuncPtr), name
