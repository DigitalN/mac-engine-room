"""The relaunch script, run for real with stand-ins for `open` and `ps`: nothing is launched."""

import os
import subprocess
import time

import launch


def _quitting_process(seconds):
    """A process that exits after `seconds`, not this one's child, so it's reaped when it does."""
    out = subprocess.run(["/bin/sh", "-c", f"sleep {seconds} >/dev/null 2>&1 & echo $!"],
                         capture_output=True, text=True, check=True).stdout
    return out.strip()


def _relaunch(tmp_path, starts_on_attempt):
    """Runs RELAUNCH_SCRIPT with an `open` that starts the app on the given attempt (or never)
    and a `ps` that lists what it started. Returns the calls to `open`."""
    calls, running = tmp_path / "calls", tmp_path / "running"
    calls.write_text("")
    running.write_text("")
    fake_open = tmp_path / "open"
    fake_open.write_text(
        '#!/bin/sh\n'
        f'echo "$*" >> "{calls}"\n'
        f'[ "$(wc -l < "{calls}")" -eq {starts_on_attempt} ] && echo "$EXE" >> "{running}"\n'
        'exit 0\n')
    # Run through /bin/sh rather than executed as a file, so macOS never assesses it.
    script = (launch.RELAUNCH_SCRIPT.replace("/usr/bin/open", f'/bin/sh "{fake_open}"')
              .replace("/bin/ps -axo comm=", f'cat "{running}"').replace("-lt 50", "-lt 5"))
    exe = launch.executable(tmp_path / "Mac Engine Room.app")
    pid = _quitting_process(0.4)
    start = time.monotonic()
    subprocess.run(["/bin/sh", "-c", script, "sh", pid, exe, "-g", "/Applications/Mac Engine Room.app",
                    "--args", "--background"], env={**os.environ, "EXE": exe}, timeout=20, check=True)
    return calls.read_text().splitlines(), time.monotonic() - start


def test_it_waits_for_the_old_copy_then_opens_a_new_instance(tmp_path):
    calls, took = _relaunch(tmp_path, starts_on_attempt=1)
    assert took >= 0.3  # waited for the quitting copy to exit first
    assert calls == ["-n -g /Applications/Mac Engine Room.app --args --background"]


def test_it_opens_again_if_the_app_didnt_start(tmp_path):
    """What happened updating 1.2.8 to 1.2.9: LaunchServices still listed the old copy, the
    open went to it, and nothing started."""
    calls, _ = _relaunch(tmp_path, starts_on_attempt=2)
    assert len(calls) == 2 and all(c.startswith("-n ") for c in calls)


def test_it_gives_up_after_two_tries(tmp_path):
    calls, _ = _relaunch(tmp_path, starts_on_attempt=0)
    assert len(calls) == 2


def test_it_waits_up_to_60_seconds_for_the_old_copy_and_only_while_it_runs(tmp_path):
    """A quit on a busy Mac can take longer than the 10 s 1.2.19 waited: it lets a fan write in
    progress finish first. Run at a tenth of the time: each step sleeps 0.01 s, not 0.1 s."""
    assert "[ $tries -lt 600 ]; do sleep 0.1;" in launch.RELAUNCH_SCRIPT  # 600 × 0.1 s
    fast = launch.RELAUNCH_SCRIPT.replace("sleep 0.1", "sleep 0.01")
    calls = tmp_path / "calls"
    fake_open = tmp_path / "open"
    fake_open.write_text(f'#!/bin/sh\nkill -0 "$OLD" 2>/dev/null && echo alive >> "{calls}" || echo gone >> "{calls}"\n')
    script = (fast.replace("/usr/bin/open", f'/bin/sh "{fake_open}"')
              .replace("/bin/ps -axo comm=", 'echo "$exe"'))  # the new copy is running at once
    exe = launch.executable(tmp_path / "Mac Engine Room.app")

    def run(old):
        calls.write_text("")
        start = time.monotonic()
        subprocess.run(["/bin/sh", "-c", script, "sh", old, exe, "/Applications/Mac Engine Room.app"],
                       env={**os.environ, "OLD": old}, timeout=30, check=True)
        return calls.read_text().split(), time.monotonic() - start

    seen, took = run(_quitting_process(2.5))  # longer than 100 steps (1 s here, 10 s for real), within 600
    assert seen == ["gone"] and took >= 2.0
    seen, took = run(_quitting_process(0))  # already gone: no wait
    assert seen == ["gone"] and took < 1.0


def test_the_check_looks_for_the_apps_own_executable(tmp_path):
    app = tmp_path / "Mac Engine Room.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        b'<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleExecutable</key>'
        b'<string>Mac Engine Room</string></dict></plist>')
    # Not the fan helper, which runs from the same folder as root.
    assert launch.executable(app) == str(app / "Contents" / "MacOS" / "Mac Engine Room")
    assert launch.executable(tmp_path / "Pythonflow.app").endswith("Pythonflow.app/Contents/MacOS/Pythonflow")
