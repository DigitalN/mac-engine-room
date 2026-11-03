"""Where Mac Engine Room runs from, relaunching it, and opening it at login.

Mac Engine Room has to run from an Applications folder. Opened straight from the
downloaded disk image or the Downloads folder, macOS runs a temporary hidden copy
("app translocation"): a login item would point at that copy, and the app can't
replace itself with an update. On launch from anywhere else, it offers to move.

Everything here is a no-op when running from source (`python3 main.py`), where the
"app" is Python itself.
"""

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("pythonflow")

REPOSITORY = "DigitalN/mac-engine-room"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
# Passed when relaunching into an update, so the dashboard doesn't pop up on its own.
BACKGROUND_ARGUMENT = "--background"

# Waits for the quitting copy to exit, opens the app, and checks that it's running, trying
# once more if it isn't after 5 seconds. `open -n`: LaunchServices can still list an app as
# running for a moment after it has quit, and a plain `open` then goes to the instance that's
# gone, so nothing starts (the maintainer's update from 1.2.8 to 1.2.9). The check matches
# the app's own executable exactly, not the fan helper in the same bundle.
RELAUNCH_SCRIPT = (
    'pid=$1; exe=$2; shift 2; tries=0\n'
    'while kill -0 "$pid" 2>/dev/null && [ $tries -lt 100 ]; do sleep 0.1; tries=$((tries + 1)); done\n'
    'for attempt in 1 2; do\n'
    '  /usr/bin/open -n "$@"\n'
    '  tries=0\n'
    '  while [ $tries -lt 50 ]; do\n'
    '    /bin/ps -axo comm= | /usr/bin/grep -qxF -- "$exe" && exit 0\n'
    '    sleep 0.1; tries=$((tries + 1))\n'
    '  done\n'
    'done\n'
)

# SMAppServiceStatus
_ENABLED = 1
# The "open application" Apple event macOS sends at launch carries this property
# when loginwindow opened the app as a login item.
_OPEN_APPLICATION = int.from_bytes(b"oapp", "big")  # kAEOpenApplication
_PROPERTY_DATA = int.from_bytes(b"prdt", "big")  # keyAEPropData
_LAUNCHED_AS_LOGIN_ITEM = int.from_bytes(b"lgit", "big")  # keyAELaunchedAsLogInItem


def bundle_path():
    """The running app bundle, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    from Foundation import NSBundle

    return Path(NSBundle.mainBundle().bundlePath())


def in_applications_folder(path):
    resolved = str(Path(path).resolve())
    folders = ("/Applications/", str(Path.home() / "Applications") + "/")
    return resolved.startswith(folders)


def launched_at_login():
    """True when macOS opened the app as a login item. Only meaningful while the launch's
    "open application" event is being handled, i.e. as the app finishes launching."""
    from Foundation import NSAppleEventManager

    event = NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
    if event is None or event.eventID() != _OPEN_APPLICATION:
        return False
    reason = event.paramDescriptorForKeyword_(_PROPERTY_DATA)
    return reason is not None and reason.enumCodeValue() == _LAUNCHED_AS_LOGIN_ITEM


def activate_app():
    import AppKit

    app = AppKit.NSApp
    if hasattr(app, "activate"):
        app.activate()
    else:
        app.activateIgnoringOtherApps_(True)


def offer_to_move(source):
    """Asks to move the app into Applications. Returns True if it's relaunching from there."""
    import AppKit
    from Foundation import NSURL, NSFileManager

    activate_app()
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Move Mac Engine Room to your Applications folder?")
    alert.setInformativeText_("Mac Engine Room needs to be in Applications to open at login and keep itself up to date.")
    alert.addButtonWithTitle_("Move to Applications")
    alert.addButtonWithTitle_("Not Now")
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return False

    manager = NSFileManager.defaultManager()
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        destination = folder / source.name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                ok, _, error = manager.trashItemAtURL_resultingItemURL_error_(
                    NSURL.fileURLWithPath_(str(destination)), None, None)
                if not ok:
                    raise OSError(str(error))
            subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            log.warning("Couldn't move Mac Engine Room to %s: %s", folder, e)
            continue
        # The user already chose to open this download; don't make them approve the copy again.
        subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(destination)], capture_output=True)
        log.info("Moved Mac Engine Room to %s; relaunching", destination)
        relaunch(destination)
        return True

    failure = AppKit.NSAlert.alloc().init()
    failure.setMessageText_("Mac Engine Room couldn't move itself")
    failure.setInformativeText_("Quit Mac Engine Room, drag it into your Applications folder in Finder, then open it from there.")
    failure.runModal()
    return False


def relaunch(app, background=False, arguments=()):
    """Opens `app` once this process has exited, passing it `arguments`. The caller
    quits right after.

    Waiting for the exit matters: until then this copy holds the single-instance
    lock, and the new one would quit straight away. See RELAUNCH_SCRIPT.
    """
    app_arguments = [BACKGROUND_ARGUMENT, *arguments] if background else list(arguments)
    arguments = ["-g"] if background else []
    if os.environ.get("PYTHONFLOW_DATA_DIR"):  # keep a test copy on its test folder
        arguments += ["--env", f"PYTHONFLOW_DATA_DIR={os.environ['PYTHONFLOW_DATA_DIR']}"]
    arguments.append(str(app))
    if app_arguments:
        arguments += ["--args", *app_arguments]
    subprocess.Popen(["/bin/sh", "-c", RELAUNCH_SCRIPT, "sh", str(os.getpid()), executable(app), *arguments],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def executable(app):
    """The app's main executable, as `ps` shows it once it runs."""
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
    except (OSError, plistlib.InvalidFileException):
        name = None
    return str(Path(app) / "Contents" / "MacOS" / (name or Path(app).stem))


class LoginItem:
    """"Open at login", through ServiceManagement's SMAppService (macOS 13+).

    Only available to the app itself running from Applications: the login item
    points at wherever the app is when it's turned on.
    """

    def __init__(self, app_path, data_dir):
        self._service = None
        self._marker = Path(data_dir) / ".open-at-login-default-applied"
        if app_path is None or not in_applications_folder(app_path):
            return
        try:
            import objc

            objc.loadBundle("ServiceManagement", {},
                            bundle_path="/System/Library/Frameworks/ServiceManagement.framework")
            service_class = objc.lookUpClass("SMAppService")
            for selector in (b"registerAndReturnError:", b"unregisterAndReturnError:"):
                objc.registerMetaDataForSelector(b"SMAppService", selector,
                                                 {"arguments": {2: {"type_modifier": b"o"}}})
            self._service = service_class.mainAppService()
        except Exception:
            log.exception("Open at login isn't available")

    @property
    def available(self):
        return self._service is not None

    @property
    def enabled(self):
        return self.available and self._service.status() == _ENABLED

    def set_enabled(self, on):
        if not self.available or on == self.enabled:
            return
        if on:
            ok, error = self._service.registerAndReturnError_(None)
        else:
            ok, error = self._service.unregisterAndReturnError_(None)
        if ok:
            log.info("Open at login turned %s", "on" if on else "off")
        else:
            log.warning("Couldn't turn open at login %s: %s", "on" if on else "off", error)

    def repoint(self):
        """After an old copy was replaced, registers the login item again from here."""
        if self.enabled:
            self._service.unregisterAndReturnError_(None)
            self.set_enabled(True)

    def turn_on_by_default(self):
        """Open at login starts out on. It's only switched on once, so turning it off sticks."""
        if not self.available or self._marker.exists():
            return
        self.set_enabled(True)
        self._marker.parent.mkdir(parents=True, exist_ok=True)
        self._marker.touch()
