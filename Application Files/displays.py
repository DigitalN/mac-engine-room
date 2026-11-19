"""Displays: brightness for each display on its own, the built-in XDR display's boost past 100%, and the
brightness, volume and mute keys.

The work is in Swift (Native/Displays, built into Contents/Frameworks/libdisplays.dylib): the built-in
display through DisplayServices, external displays and TVs over DDC on a queue of its own, the boost's
EDR trigger and gamma table, the key tap on a thread of its own, and the HUD. This module loads the
dylib with ctypes, passes it the menu's and the page's values, remembers the boost (`display_boost`)
and turns the keys on and off (`display_keys`). A build without a Swift compiler has no dylib: the
menu has no Displays section and the tab says so.

The key tap is active, so it needs Accessibility (accessibility.py): the first time the switch is
turned on while it isn't allowed, macOS is asked; the tap starts as soon as it's allowed and stops if
it's taken away. There's no tap while the switch is off, DDC is only used on a change or when the
menu or the tab opens, and the boost's trigger only exists above 100%.
"""

import ctypes
import json
import logging
import sys
from pathlib import Path

import accessibility

log = logging.getLogger("pythonflow")

# Chips whose displays have ignored gamma tables since macOS 26.3, built-in and external alike (still
# on 27.2.1; Apple forum thread 819331, FB22273730), so the boost can't work there. The base M5 works.
CHIPS_IGNORING_GAMMA = frozenset({"Apple M5 Pro", "Apple M5 Max", "Apple A18 Pro"})  # A18 Pro: the MacBook Neo
SAVE_BOOST_AFTER = 1.0  # seconds: dragging a slider or holding a key saves once, at the end

# displays_set_keys' answers (Native/Displays/Displays.swift).
ON, OFF, NOT_ALLOWED, REFUSED = 1, 0, -1, -2


def library_path():
    """Contents/Frameworks/libdisplays.dylib in the app, or None from source (no dylib is built there)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent.parent / "Frameworks" / "libdisplays.dylib"


def chip_ignores_gamma(chip):
    return (chip or "").strip() in CHIPS_IGNORING_GAMMA


def builtin(displays):
    return next((d for d in displays if d.get("builtin")), None)


def boost_of(displays):
    """What `display_boost` should say for the built-in display as it is now: its percent over 100,
    or 1.0 (no boost, or no built-in display that boosts)."""
    d = builtin(displays)
    if not d or not d.get("percent") or d.get("top", 100) <= 100:
        return 1.0
    return round(min(d["top"], max(100.0, d["percent"])) / 100, 4)


def can_bring_back(displays, boost):
    """At launch: the saved boost comes back only onto a built-in display that boosts, with macOS's own
    brightness at its maximum (anything that lowered it ended the boost)."""
    d = builtin(displays)
    return bool(boost > 1 and d and d.get("top", 100) > 100 and (d.get("percent") or 0) >= 99.95)


def should_ask(was_on, now_on, allowed, asked):
    """Ask macOS for Accessibility when the keys switch has just been turned on, it isn't allowed, and
    macOS hasn't been asked since launch. Later, the tab's button opens the pane instead."""
    return now_on and not was_on and not allowed and not asked


def menu_label(display):
    """The menu's and the page's percent: "130%", "42.5%" after a quarter step, "—" when unknown."""
    percent = display.get("percent")
    if percent is None:
        return "—"
    return f"{percent:.1f}%" if round(percent, 1) != round(percent) else f"{round(percent)}%"


class Library:
    """libdisplays.dylib's C functions. Its callbacks come on the main thread."""

    CHANGED = ctypes.CFUNCTYPE(None)
    LOG = ctypes.CFUNCTYPE(None, ctypes.c_char_p)

    def __init__(self, path):
        lib = ctypes.CDLL(str(path))
        lib.displays_start.argtypes = [ctypes.c_bool, self.CHANGED, self.LOG]
        lib.displays_start.restype = None
        lib.displays_list.argtypes = []
        lib.displays_list.restype = ctypes.c_void_p  # freed with displays_free
        lib.displays_free.argtypes = [ctypes.c_void_p]
        lib.displays_free.restype = None
        lib.displays_refresh.argtypes = []
        lib.displays_refresh.restype = None
        lib.displays_set_brightness.argtypes = [ctypes.c_uint32, ctypes.c_double]
        lib.displays_set_brightness.restype = None
        lib.displays_set_volume.argtypes = [ctypes.c_uint32, ctypes.c_double]
        lib.displays_set_volume.restype = None
        lib.displays_set_keys.argtypes = [ctypes.c_bool]
        lib.displays_set_keys.restype = ctypes.c_int32
        lib.displays_stop_keys.argtypes = []
        lib.displays_stop_keys.restype = None
        lib.displays_keys_running.argtypes = []
        lib.displays_keys_running.restype = ctypes.c_bool
        self._lib = lib
        self._callbacks = ()  # kept: ctypes doesn't

    def start(self, chip_ignores_gamma, changed, log_line):
        self._callbacks = (self.CHANGED(changed), self.LOG(lambda line: log_line(line.decode("utf-8", "replace"))))
        self._lib.displays_start(chip_ignores_gamma, *self._callbacks)

    def list(self):
        pointer = self._lib.displays_list()
        if not pointer:
            return []
        try:
            return json.loads(ctypes.string_at(pointer).decode("utf-8"))
        finally:
            self._lib.displays_free(pointer)

    def refresh(self):
        self._lib.displays_refresh()

    def set_brightness(self, display, percent):
        self._lib.displays_set_brightness(display, percent)

    def set_volume(self, display, percent):
        self._lib.displays_set_volume(display, percent)

    def set_keys(self, on):
        return self._lib.displays_set_keys(on)

    def stop_keys(self):
        self._lib.displays_stop_keys()

    def keys_running(self):
        return bool(self._lib.displays_keys_running())


class Displays:
    """Runs the dylib from Python: main thread, except `settings_changed`, `displays` and `status`, which
    can come from any thread. `on_change(listener)` listeners run on the main thread whenever a display
    changed (a key press, a slider, DDC answering, the boost ending)."""

    def __init__(self, settings, library=None, access=accessibility, call_on_main=None, call_later=None, chip=None):
        self._settings = settings
        self._library = library  # a Library, or None until start() loads it (None too without a dylib)
        self._access = access
        self._call_on_main = call_on_main
        self._call_later = call_later
        self._chip = chip
        self._list = []  # the displays as the dylib last said (read on the main thread, shared)
        self._keys = bool(settings.get()["display_keys"])
        self._asked = False
        self._allowed = None  # last known grant
        self._paused = False  # the tap stopped for the updater's swap
        self._shown = None  # what app.log last said about the keys
        self._boost_saved = self._boost_wanted = settings.get()["display_boost"]
        self._save_pending = False
        self._started = False  # the saved boost has been brought back (or let go): from here it's saved
        self._listeners = []

    @property
    def available(self):
        return self._library is not None

    def on_change(self, listener):
        self._listeners.append(listener)

    def start(self):
        """At launch, on the main thread: load the dylib, bring the boost back if it can be, make the tap
        if the switch is on and it's allowed (never asking at launch), and follow the grant."""
        if self._library is None:
            path = library_path()
            if path is None or not path.exists():
                log.info("Displays isn't available: %s", "running from source" if path is None else "this build has no libdisplays.dylib")
                return
            try:
                self._library = Library(path)
            except OSError as e:
                log.warning("Displays isn't available: couldn't load libdisplays.dylib (%s)", e)
                return
        chip = self._chip
        if chip is None:
            import thermals

            chip = thermals.chip_name()
        self._library.start(chip_ignores_gamma(chip), self._changed, log.info)
        self._list = self._library.list()
        self._bring_back_boost()
        self._started = True
        self._access.watch(self._grant_changed)
        self._access.register(self.stop_tap, self.start_tap)
        self._apply_keys()

    def _bring_back_boost(self):
        boost = self._settings.get()["display_boost"]
        if boost <= 1:
            return
        if can_bring_back(self._list, boost):
            self._library.set_brightness(builtin(self._list)["id"], boost * 100)
            self._list = self._library.list()
            log.info("Display boost brought back at %d%%", round(boost * 100))
        else:
            log.info("Display boost not brought back: %s", "macOS's brightness is below its maximum"
                     if builtin(self._list) and builtin(self._list).get("top", 100) > 100 else "this Mac's display can't boost")
            self._save_boost(1.0, at_once=True)

    # ── displays ──

    def displays(self):
        return list(self._list)

    def refresh(self):
        """The menu or the tab opened: read the displays again (DDC answers come later)."""
        if self._library is not None:
            self._library.refresh()

    def set_brightness(self, display, percent):
        """The menu's or the page's slider."""
        if self._library is None:
            return
        self._library.set_brightness(int(display), float(percent))
        self._changed()

    def set_volume(self, display, percent):
        if self._library is None:
            return
        self._library.set_volume(int(display), float(percent))
        self._changed()

    def _changed(self):
        """Main thread (the dylib's callback, or a slider): what the displays are now; the boost is saved
        a second after it last changed."""
        self._list = self._library.list()
        boost = boost_of(self._list)
        if self._started and builtin(self._list) and boost != self._boost_saved:
            self._save_boost(boost)
        for listener in list(self._listeners):
            try:
                listener(self._list)
            except Exception:
                log.exception("Displays listener failed")

    def _save_boost(self, boost, at_once=False):
        self._boost_wanted = boost
        if at_once:
            self._flush_boost()
        elif not self._save_pending:
            self._save_pending = True
            self._later(SAVE_BOOST_AFTER, self._flush_boost)

    def _flush_boost(self):
        self._save_pending = False
        boost = self._boost_wanted
        if boost != self._boost_saved:
            self._boost_saved = boost
            self._settings.update({"display_boost": boost})

    # ── the keys ──

    def settings_changed(self, values):
        """Any thread: the settings were saved. The switch just turned on asks for Accessibility."""
        keys = bool(values.get("display_keys"))
        if keys != self._keys:
            self._main(lambda: self._switch(keys))

    def _switch(self, keys):
        was, self._keys = self._keys, keys
        if self.available and should_ask(was, keys, self._access.allowed(), self._asked):
            self._asked = True
            log.info("Asked macOS to allow Accessibility for the brightness and volume keys")
            self._access.ask()
        self._apply_keys()

    def _grant_changed(self, allowed):
        """Main thread: Accessibility may have been allowed or taken away."""
        if allowed == self._allowed:
            return
        if not allowed and self._library is not None and self._library.keys_running():
            self._library.stop_keys()  # macOS no longer sends it events; drop it rather than leave it
        self._apply_keys(allowed)

    def stop_tap(self):
        """The updater, just before it swaps the bundle (see accessibility.py)."""
        self._paused = True
        if self._library is not None and self._library.keys_running():
            self._library.stop_keys()
            self._show("Display keys tap stopped for the update")

    def start_tap(self):
        """The updater's swap failed: back as the switch says."""
        self._paused = False
        self._apply_keys()

    def _apply_keys(self, allowed=None):
        if self._library is None or self._paused:
            return
        self._allowed = self._access.allowed() if allowed is None else allowed
        if not self._keys:
            self._library.set_keys(False)
            self._show(None)
            return
        if not self._allowed:
            self._library.stop_keys()
            self._show("Display keys are waiting for Accessibility to be allowed")
            return
        result = self._library.set_keys(True)
        if result == ON:
            self._show("Display keys on: brightness, volume and mute")
        elif result == NOT_ALLOWED:  # the grant went between reading it and making the tap
            self._allowed = False
            self._show("Display keys are waiting for Accessibility to be allowed")
        else:
            self._show("Display keys tap couldn't be made: macOS refused it")

    def _show(self, line):
        """app.log, once per change. None: the switch is off."""
        if line is None:
            line = "Display keys off" if self._shown is not None else None
        if line is not None and line != self._shown:
            log.info(line)
        self._shown = line

    def status(self):
        """For the Displays tab: the displays, whether this build has the dylib, whether Accessibility is
        allowed (read now: the tab polls it) and whether the tap is running. A grant that changed without
        its notification is acted on from here."""
        if self._library is None:
            return {"available": False, "displays": [], "allowed": False, "running": False}
        allowed = self._access.allowed()
        if allowed != self._allowed:
            self._main(lambda: self._grant_changed(allowed))
        return {"available": True, "displays": self.displays(), "allowed": allowed,
                "running": self._library.keys_running()}

    def _main(self, fn):
        if self._call_on_main is not None:
            self._call_on_main(fn)
        else:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(fn)

    def _later(self, delay, fn):
        if self._call_later is not None:
            self._call_later(delay, fn)
        else:
            from PyObjCTools import AppHelper

            AppHelper.callLater(delay, fn)
