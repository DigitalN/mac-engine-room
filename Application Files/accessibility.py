"""Accessibility, which every active event tap needs (Mouse, and Displays' brightness and volume keys).

- `allowed()`: whether macOS allows this app (`AXIsProcessTrusted`).
- `ask()`: macOS's own prompt, which also adds the app to the list in Privacy & Security ›
  Accessibility. Run the first time a switch that needs a tap is turned on.
- `open_settings()`: that pane of System Settings.
- `watch(callback)`: `callback(allowed)` on the main thread whenever the grant may have changed, so a
  tap starts as soon as it's allowed and stops if it's taken away, without a relaunch. macOS posts
  the `com.apple.accessibility.api` distributed notification when the list changes, before the new
  value can be read, so the grant is read again after a short delay (twice).
- `register(stop, start)`, `stop_taps()`, `start_taps()`: the updater stops every tap just before it
  swaps the bundle and starts them again if the swap fails. On macOS 27.2 a bundle replaced under a
  running tap made tccd refuse the old process, and the keyboard and trackpad froze for about two
  minutes (Scroll Reverser #220).

The grant is TCC's, keyed to the app's designated requirement (which pins the Apple Development
certificate's name), so updates signed with the same certificate keep it.
"""

import ctypes
import logging
import platform

log = logging.getLogger("pythonflow")

SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
NOTIFICATION = "com.apple.accessibility.api"
RECHECK_AFTER = (0.5, 3.0)  # seconds after the notification: the value lags behind it


def setting_name():
    """What System Settings calls the list an event tap must be allowed in: macOS 27 renamed Privacy &
    Security › Accessibility to Device Control and Data Access (seen on 27.2.1)."""
    try:
        major = int(platform.mac_ver()[0].split(".")[0])
    except ValueError:
        major = 0
    return "Device Control and Data Access" if major >= 27 else "Accessibility"

_services = None
_watchers = []
_observer = None
_taps = []  # (stop, start)


def _hi_services():
    global _services
    if _services is None:
        lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        lib.AXIsProcessTrusted.argtypes = []
        lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        _services = lib
    return _services


def allowed():
    return bool(_hi_services().AXIsProcessTrusted())


def ask():
    """macOS's prompt to allow the app (only while it isn't allowed). Returns whether it's allowed."""
    import objc
    from Foundation import NSDictionary

    # kAXTrustedCheckOptionPrompt's value; the constant itself is in HIServices, which PyObjC isn't bundled for.
    options = NSDictionary.dictionaryWithDictionary_({"AXTrustedCheckOptionPrompt": True})
    return bool(_hi_services().AXIsProcessTrustedWithOptions(objc.pyobjc_id(options)))


def open_settings():
    """Privacy & Security › Accessibility. Main thread."""
    from AppKit import NSURL, NSWorkspace

    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(SETTINGS_URL))


def watch(callback):
    """`callback(allowed)` on the main thread when the grant may have changed. Main thread."""
    global _observer
    _watchers.append(callback)
    if _observer is not None:
        return
    from Foundation import NSDistributedNotificationCenter, NSNotificationSuspensionBehaviorDeliverImmediately

    _observer = _make_observer()
    # Delivered at once: AppKit holds distributed notifications back from an app that isn't active.
    NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(
        _observer, "changed:", NOTIFICATION, None, NSNotificationSuspensionBehaviorDeliverImmediately)


def _changed():
    from PyObjCTools import AppHelper

    for delay in RECHECK_AFTER:
        AppHelper.callLater(delay, _tell_watchers)


def _tell_watchers():
    now = allowed()
    for callback in list(_watchers):
        try:
            callback(now)
        except Exception:
            log.exception("Accessibility watcher failed")


def _make_observer():
    from Foundation import NSObject

    class AccessibilityObserver(NSObject):
        def changed_(self, notification):
            _changed()

    return AccessibilityObserver.alloc().init()


def register(stop, start):
    """A tool's taps, for the updater: `stop()` drops them, `start()` makes them again as its settings say."""
    _taps.append((stop, start))


def stop_taps():
    for stop, _ in _taps:
        try:
            stop()
        except Exception:
            log.exception("Couldn't stop an event tap")


def start_taps():
    for _, start in _taps:
        try:
            start()
        except Exception:
            log.exception("Couldn't start an event tap")
