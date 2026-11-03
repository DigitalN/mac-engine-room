"""The fan helper's unlock and hand-back (Fan Helper/Fans.swift), against a simulated SMC
(tests/fan_helper/FakeSMC.swift) that behaves like an M4, which needs Ftst, or this M1 Pro,
which has none. Skipped without a Swift compiler."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("xcrun") or subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True).returncode,
                    reason="no Swift compiler")
def test_fan_helper_unlocks_and_hands_back(tmp_path):
    exe = tmp_path / "fan-helper-tests"
    subprocess.run(["xcrun", "swiftc", str(ROOT / "Fan Helper/Fans.swift"), str(ROOT / "tests/fan_helper/FakeSMC.swift"),
                    str(ROOT / "tests/fan_helper/main.swift"), "-o", str(exe)], check=True, capture_output=True, timeout=300)
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
