"""Mouse's Swift tap (Native/Mouse): its pure parts in tests/mouse/main.swift (which devices are wheel
mice, the glide, Back and Forward, reversing an event), and the dylib the build makes, loaded the way
mouse.py loads it. Nothing here makes a tap: every switch stays off, so it needs no Accessibility and
can't touch the Mac's scrolling. Skipped without a Swift compiler."""

import ctypes
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(str(p) for p in (ROOT / "Native/Mouse").glob("*.swift"))

needs_swift = pytest.mark.skipif(
    not shutil.which("xcrun") or subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True).returncode,
    reason="no Swift compiler")


@needs_swift
def test_pure_parts(tmp_path):
    exe = tmp_path / "mouse-tests"
    subprocess.run(["xcrun", "swiftc", *SOURCES, str(ROOT / "tests/mouse/main.swift"), "-o", str(exe)],
                   check=True, capture_output=True, timeout=300)
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_swift
def test_dylib_loads_and_stays_off(tmp_path):
    """Built as scripts/build.sh builds it, and driven through mouse.Library like the app."""
    import mouse

    lib = tmp_path / "libmouse.dylib"
    subprocess.run(["xcrun", "swiftc", "-O", "-target", "arm64-apple-macos13.0", "-emit-library", "-module-name", "Mouse",
                    *SOURCES, "-o", str(lib)], check=True, capture_output=True, timeout=300)
    library = mouse.Library(lib)
    assert library.configure(False, False, False) == mouse.OFF
    assert not library.running()
    library.stop()
    assert isinstance(ctypes.CDLL(str(lib)).mouse_running, ctypes._CFuncPtr)
