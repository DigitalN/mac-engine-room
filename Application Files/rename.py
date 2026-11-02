"""Pythonflow became Mac Engine Room in 1.2.7. This moves an installed copy over, once.

- The updater swaps each new version into the installed bundle's place, so a copy that
  updated itself is still called Pythonflow.app. Its first launch renames the bundle and
  relaunches from there.
- A copy installed by hand next to the old one quits the old one and moves it to the Trash.
- ~/Library/Application Support/Pythonflow and ~/Library/Logs/Pythonflow move to the new
  names, unless those already exist.

The bundle identifier is still com.pythonflow.monitor, so the updater and every installed
copy treat both names as the same app.
"""

import fcntl
import logging
import os
import sys
import time
from pathlib import Path

from launch import BACKGROUND_ARGUMENT, relaunch
from updater import info_value

log = logging.getLogger("pythonflow")

OLD_NAME = "Pythonflow"
OLD_BUNDLE = f"{OLD_NAME}.app"
NEW_BUNDLE = "Mac Engine Room.app"
# Passed to the renamed copy when the old one was open at login: the login item is turned
# off before the rename and back on from the new place.
LOGIN_ITEM_ARGUMENT = "--open-at-login"
QUIT_TIMEOUT = 10  # seconds to wait for an old copy to quit


def _lock_is_free(folder):
    """Whether no running copy holds the single-instance lock in `folder`."""
    try:
        fd = os.open(Path(folder) / ".lock", os.O_RDONLY)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _quit_other_copies(bundle_id):
    """Asks every other running copy of this app to quit. Returns where they ran from."""
    from AppKit import NSRunningApplication

    own = os.getpid()
    others = [app for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
              if app.processIdentifier() != own]
    for app in others:
        app.terminate()
    return [str(app.bundleURL().path()) if app.bundleURL() else str(app.processIdentifier()) for app in others]


def move_old_folders(moves, bundle_id, timeout=QUIT_TIMEOUT):
    """Moves each (old, new) folder pair whose old folder exists and new one doesn't.

    The first pair is the data folder. If a copy of the old version still holds its
    lock, that copy is asked to quit first. Returns what happened, to log once logging
    is set up, or None if the old copy is still running."""
    pending = [(Path(old), Path(new)) for old, new in moves if Path(old).is_dir() and not Path(new).exists()]
    if not pending:
        return []
    notes = []
    data = Path(moves[0][0])
    if any(old == data for old, _ in pending) and not _lock_is_free(data):
        if bundle_id:
            notes += [f"Quit the old copy at {where}" for where in _quit_other_copies(bundle_id)]
        deadline = time.monotonic() + timeout
        while not _lock_is_free(data):
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)
    for old, new in pending:
        try:
            os.rename(old, new)
            notes.append(f"Moved {old} to {new}")
        except OSError as e:
            notes.append(f"Couldn't move {old} to {new}: {e}")
    return notes


def _trash(path):
    from Foundation import NSURL, NSFileManager

    ok, _, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    if not ok:
        log.warning("Couldn't move %s to the Trash: %s", path, error)
    return bool(ok)


def rename_bundle(app_path, login_item):
    """Renames a copy still called Pythonflow.app and relaunches it from its new name.
    Returns True if it's relaunching."""
    if app_path.name != OLD_BUNDLE:
        return False
    target = app_path.with_name(NEW_BUNDLE)
    if target.exists():
        if info_value(target, "CFBundleIdentifier") != info_value(app_path, "CFBundleIdentifier"):
            log.warning("Not renaming %s: %s is another app", app_path, target)
            return False
        if not _trash(target):
            return False
        log.info("Moved the other copy at %s to the Trash", target)
    was_open_at_login = login_item.enabled
    login_item.set_enabled(False)  # it points at the old name
    try:
        os.rename(app_path, target)
    except OSError as e:
        log.warning("Couldn't rename %s: %s", app_path, e)
        login_item.set_enabled(was_open_at_login)
        return False
    log.info("Renamed %s to %s; relaunching", app_path, target.name)
    relaunch(target, background=BACKGROUND_ARGUMENT in sys.argv,
             arguments=[LOGIN_ITEM_ARGUMENT] if was_open_at_login else [])
    return True


def trash_old_copies(app_path, folders=None):
    """Moves copies of this app still called Pythonflow.app to the Trash. Returns True if
    there were any."""
    bundle_id = info_value(app_path, "CFBundleIdentifier")
    folders = folders or (Path("/Applications"), Path.home() / "Applications")
    trashed = False
    for folder in folders:
        old = Path(folder) / OLD_BUNDLE
        if old.resolve() == Path(app_path).resolve() or info_value(old, "CFBundleIdentifier") != bundle_id:
            continue
        if _trash(old):
            log.info("Moved the old copy at %s to the Trash", old)
            trashed = True
    return trashed
