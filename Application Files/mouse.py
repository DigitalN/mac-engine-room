"""Mouse: reverse scrolling for wheel mice, smooth scrolling, and the side buttons as Back and Forward.

The work is an event tap in Swift (Native/Mouse, built into Contents/Frameworks/libmouse.dylib): it
sits in the path of every scroll event, so it can't wait for Python's threads, which share the GIL
and sit at background QoS while idle. This module loads the dylib with ctypes and only turns the tap
on and off. A build without a Swift compiler has no dylib, and the Mouse tab hides its switches.

The tap is active, so it needs Accessibility (accessibility.py): the first time a switch is turned on
while it isn't allowed, macOS is asked; the tap starts as soon as it's allowed and stops if it's
taken away. There's no tap at all while every switch is off, and pointer acceleration is macOS's own
setting (the tab links to it).
"""

import ctypes
import logging
import sys
from pathlib import Path

import accessibility

log = logging.getLogger("pythonflow")

SWITCHES = ("mouse_reverse_scrolling", "mouse_smooth_scrolling", "mouse_back_forward")  # settings.py's keys
NAMES = {"mouse_reverse_scrolling": "reverse scrolling", "mouse_smooth_scrolling": "smooth scrolling",
         "mouse_back_forward": "back and forward buttons"}
# System Settings › Mouse, where pointer acceleration is (Advanced…, mice only).
POINTER_SETTINGS_URL = "x-apple.systempreferences:com.apple.Mouse-Settings.extension"

# mouse_configure's answers (Native/Mouse/Mouse.swift).
ON, OFF, NOT_ALLOWED, REFUSED = 1, 0, -1, -2


def library_path():
    """Contents/Frameworks/libmouse.dylib in the app, or None from source (no dylib is built there)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent.parent / "Frameworks" / "libmouse.dylib"


def switches(values):
    """The three switches from settings, in SWITCHES' order."""
    return tuple(bool(values.get(key)) for key in SWITCHES)


def describe(on):
    """What the tap does, for app.log: "reverse scrolling, back and forward buttons"."""
    return ", ".join(NAMES[key] for key, value in zip(SWITCHES, on) if value)


def should_ask(before, after, allowed, asked):
    """Pure: ask macOS for Accessibility when a switch has just been turned on, it isn't allowed, and
    macOS hasn't been asked since launch. Later, the tab's button opens the pane instead."""
    turned_on = any(a and not b for b, a in zip(before, after))
    return turned_on and not allowed and not asked


class Library:
    """libmouse.dylib's three C functions."""

    def __init__(self, path):
        lib = ctypes.CDLL(str(path))
        lib.mouse_configure.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool]
        lib.mouse_configure.restype = ctypes.c_int32
        lib.mouse_stop.argtypes = []
        lib.mouse_stop.restype = None
        lib.mouse_running.argtypes = []
        lib.mouse_running.restype = ctypes.c_bool
        self._lib = lib

    def configure(self, reverse, smooth, back_forward):
        return self._lib.mouse_configure(reverse, smooth, back_forward)

    def stop(self):
        self._lib.mouse_stop()

    def running(self):
        return bool(self._lib.mouse_running())


class Mouse:
    """Turns the tap on and off as the settings and the Accessibility grant change. Main thread, except
    `settings_changed` and `status`, which can come from any thread."""

    def __init__(self, settings, library=None, access=accessibility, call_on_main=None):
        self._settings = settings
        self._library = library  # a Library, or None until start() loads it (None too without a dylib)
        self._access = access
        self._call_on_main = call_on_main
        self._switches = switches(settings.get())
        self._asked = False
        self._allowed = None  # last known grant
        self._paused = False  # stopped for the updater's swap
        self._shown = None  # what app.log last said, so each change is logged once

    @property
    def available(self):
        return self._library is not None

    def start(self):
        """At launch, on the main thread: load the dylib, make the tap if a switch is on and it's
        allowed (never asking at launch), and follow the grant."""
        if self._library is None:
            path = library_path()
            if path is None or not path.exists():
                log.info("Mouse isn't available: %s", "running from source" if path is None else "this build has no libmouse.dylib")
                return
            try:
                self._library = Library(path)
            except OSError as e:
                log.warning("Mouse isn't available: couldn't load libmouse.dylib (%s)", e)
                return
        self._access.watch(self._grant_changed)
        self._access.register(self.stop_tap, self.start_tap)
        self._apply()

    def settings_changed(self, values):
        """Any thread: the settings were saved. A switch just turned on asks for Accessibility."""
        new = switches(values)
        if new != self._switches:
            self._main(lambda: self._switch(new))

    def _switch(self, new):
        before, self._switches = self._switches, new
        if self.available and should_ask(before, new, self._access.allowed(), self._asked):
            self._asked = True
            log.info("Asked macOS to allow Accessibility for the mouse switches")
            self._access.ask()
        self._apply()

    def _grant_changed(self, allowed):
        """Main thread: Accessibility may have been allowed or taken away."""
        if allowed == self._allowed:
            return
        if not allowed and self._library is not None and self._library.running():
            self._library.stop()  # macOS no longer sends it events; drop it rather than leave it
        self._apply(allowed)

    def stop_tap(self):
        """The updater, just before it swaps the bundle (see accessibility.py)."""
        self._paused = True
        if self._library is not None and self._library.running():
            self._library.stop()
            self._show("Mouse tap stopped for the update")

    def start_tap(self):
        """The updater's swap failed: back as the settings say."""
        self._paused = False
        self._apply()

    def _apply(self, allowed=None):
        if self._library is None or self._paused:
            return
        self._allowed = self._access.allowed() if allowed is None else allowed
        if not any(self._switches):
            self._library.configure(False, False, False)
            self._show(None)
            return
        if not self._allowed:
            self._library.stop()
            self._show(f"Mouse is waiting for Accessibility to be allowed ({describe(self._switches)})")
            return
        result = self._library.configure(*self._switches)
        if result == ON:
            self._show(f"Mouse tap on ({describe(self._switches)})")
        elif result == NOT_ALLOWED:  # the grant went between reading it and making the tap
            self._allowed = False
            self._show(f"Mouse is waiting for Accessibility to be allowed ({describe(self._switches)})")
        else:
            self._show("Mouse tap couldn't be made: macOS refused it")

    def _show(self, line):
        """app.log, once per change. None: every switch is off."""
        if line is None:
            line = "Mouse tap off (every switch is off)" if self._shown is not None else None
        if line is not None and line != self._shown:
            log.info(line)
        self._shown = line

    def status(self):
        """For the Mouse tab: whether the switches can work in this build, whether Accessibility is
        allowed (read now: the tab polls it), whether the tap is running, and what System Settings
        calls the list it's allowed in (`setting`). A grant that changed without its notification is
        acted on from here."""
        setting = self._access.setting_name()
        if self._library is None:
            return {"available": False, "allowed": False, "running": False, "setting": setting}
        allowed = self._access.allowed()
        if allowed != self._allowed:
            self._main(lambda: self._grant_changed(allowed))
        return {"available": True, "allowed": allowed, "running": self._library.running(), "setting": setting}

    def _main(self, fn):
        if self._call_on_main is not None:
            self._call_on_main(fn)
        else:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(fn)
