"""Mac Engine Room (formerly Pythonflow) — a menu bar thermals and power monitor for macOS.

Launcher: logging, single-instance lock, sampler thread, menu bar item and the
pywebview dashboard window, which is only made the first time the dashboard opens.

There is deliberately no HTTP server: the dashboard page talks to Python over
pywebview's in-process bridge, so the app opens no network ports. Its only network
request is the daily update check (updater.py). All data stays in
~/Library/Application Support/Mac Engine Room.
"""

import ctypes
import fcntl
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "Mac Engine Room"
OLD_NAME = "Pythonflow"  # its name until 1.2.7; see rename.py
VERSION = "1.2.19"  # the build reads it from here; see the release checklist in docs/DEVELOPMENT.md
if os.environ.get("PYTHONFLOW_DATA_DIR"):  # for development/testing against a throwaway folder
    DATA_DIR = Path(os.environ["PYTHONFLOW_DATA_DIR"]).expanduser()
    LOG_DIR = DATA_DIR / "Logs"
    LEGACY_DIR = DATA_DIR  # tests put copies of the old app's files here
else:
    DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
    LOG_DIR = Path.home() / "Library" / "Logs" / APP_NAME
    LEGACY_DIR = Path.home() / "Library" / "Application Support" / "Powerflow"
# Moved to the new names once (rename.py). Not with PYTHONFLOW_DATA_DIR, so a test copy
# never touches the real folders.
OLD_FOLDERS = [] if os.environ.get("PYTHONFLOW_DATA_DIR") else [
    (Path.home() / "Library" / "Application Support" / OLD_NAME, DATA_DIR),
    (Path.home() / "Library" / "Logs" / OLD_NAME, LOG_DIR),
]
LOG_PATH = LOG_DIR / "app.log"
# Files written by the original Rust app (Powerflow 0.2.x), imported read-only on first launch.
LEGACY_DB = LEGACY_DIR / "db.sqlite"
LEGACY_PREFS = LEGACY_DIR / "tauri-plugin-pinia" / "preference.json"
# Sampling is the app's whole idle cost, so it only runs fast while someone is looking.
DASHBOARD_INTERVAL = 1.0  # seconds, while the dashboard page is visible
BACKGROUND_INTERVAL = 2.5  # seconds, menu bar only
DASHBOARD_IDLE_AFTER = 3.0  # seconds without a poll from the page before dropping back
LIVE_BUFFER_POINTS = 900  # 15 minutes at the dashboard interval
THERMAL_FIELDS = ["t", "cpu", "gpu", "memory", "ssd", "ambient"]
FAN_PAGES = ("dashboard", "thermals")  # the page's tabs that show the fans
# The dashboard opens as tall as its first page (Dashboard), or the screen, whichever is
# smaller. The page then reports its exact size (Api.set_content_size); this just avoids a jump.
WINDOW_WIDTH = 1060
FIRST_PAGE_HEIGHT = 421
# A frame size, title bar included. Lower than the Dashboard, so it never has empty space under it.
MIN_WINDOW_SIZE = (860, 400)
# System Settings → Battery → Charging, where macOS's charge limit (80–100%) is set.
# "charging" is that sheet's anchor in the Battery pane; "batteryhealth" opens Battery Health.
CHARGING_SETTINGS_URL = "x-apple.systempreferences:com.apple.Battery-Settings.extension?charging"

log = logging.getLogger("pythonflow")


def _app_files_dir():
    """Directory holding index.html — the PyInstaller bundle when frozen, else this folder."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])
    logging.getLogger("pywebview").setLevel(logging.WARNING)


def _acquire_single_instance_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(DATA_DIR / ".lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle  # held open (and locked) for the life of the process


def _is_webkit_gpu_process(pid):
    path = ctypes.create_string_buffer(4096)
    found = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidpath(pid, path, len(path)) > 0
    return found and path.value.endswith(b"/com.apple.WebKit.GPU")


SLOW_FIRST_READING = 2.0  # seconds from launch to the first reading in the menu bar above which app.log says so
_began = None  # seconds from the process starting to main() running: the first launch of a copy waits for macOS


def first_reading_note(age, began=None, read=None):
    """The line for app.log when the first reading reached the menu bar `age` seconds after launch,
    or None. `read`: when the sampler had it, which tells a slow reading from a main thread that
    couldn't show it yet (#59: read at 9.6 s, shown at 22.2 s). `began`: how much of `age` was before
    the app's own code began to run, which isn't the app's wait: a copy that had never run once sat at
    exec for 224 s, with macOS's security scan busy."""
    if age <= SLOW_FIRST_READING:
        return None
    notes = []
    if read is not None:
        notes.append(f"read at {read:.1f} s")
    if began is not None and began > SLOW_FIRST_READING:
        notes.append(f"{began:.1f} s of it before the app's code began to run")
    return f"First reading {age:.1f} s after launch" + (f" ({'; '.join(notes)})" if notes else "")


def _seconds_since_launch():
    """Seconds since this process started, from macOS's own record of it (in mach absolute
    time, the clock time.monotonic() reads), or None."""
    class Info(ctypes.Structure):  # rusage_info_v0
        _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
            (name, ctypes.c_uint64) for name in (
                "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size",
                "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime")]

    class Timebase(ctypes.Structure):
        _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    info, timebase = Info(), Timebase()
    if lib.proc_pid_rusage(os.getpid(), 0, ctypes.byref(info)) != 0 or lib.mach_timebase_info(ctypes.byref(timebase)) != 0:
        return None
    return time.monotonic() - info.proc_start_abstime * timebase.numer / timebase.denom / 1e9


class LiveState:
    """Latest power and thermals samples plus a ring buffer of chart points for each, shared
    with the page."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._latest = None
        self._points = deque(maxlen=LIVE_BUFFER_POINTS)
        self._thermals = None
        self._thermal_points = deque(maxlen=LIVE_BUFFER_POINTS)

    def push(self, s, thermals=None):
        point = [round(s["t"], 2), s["system_load"], s["system_in"], s["battery_power"],
                 s["screen_power"], s["chip_power"]]
        with self._lock:
            self._seq += 1
            self._latest = s
            self._points.append((self._seq, point))
            if thermals is not None:
                self._thermals = thermals
                self._thermal_points.append(
                    (self._seq, [round(thermals["t"], 2)] + [thermals[k] for k in THERMAL_FIELDS[1:]]))

    def latest(self):
        with self._lock:
            return self._latest

    def since(self, seq):
        with self._lock:
            return {
                "seq": self._seq,
                "latest": self._latest,
                "point_fields": ["t", "system_load", "system_in", "battery_power", "screen_power", "chip_power"],
                "points": [p for n, p in self._points if n > seq],
                "thermals": self._thermals,
                "thermal_fields": THERMAL_FIELDS,
                "thermal_points": [p for n, p in self._thermal_points if n > seq],
            }


class Sampler(threading.Thread):
    """Reads power every tick, and in the same tick the thermals parts `thermal_parts()`
    names (see EngineRoomApp._thermal_parts). `fans_running`: a fan profile is in charge, so
    the curve needs a reading every second. Its waits are what tell `priority` the Mac is busy."""

    def __init__(self, reader, on_sample, thermals=None, thermal_parts=lambda: (),
                 fans_running=lambda: False, priority=None):
        super().__init__(name="pythonflow-sampler", daemon=True)
        self._reader = reader
        self._on_sample = on_sample
        self._thermals = thermals
        self._thermal_parts = thermal_parts
        self._fans_running = fans_running
        self._priority = priority  # priority.Priority: this thread runs at the app's one priority
        self._last_poll = float("-inf")
        self._last_fans_poll = float("-inf")
        self._wake = threading.Event()

    def dashboard_watched(self):
        return time.monotonic() - self._last_poll < DASHBOARD_IDLE_AFTER

    def interval(self):
        fast = self.dashboard_watched() or self._fans_running()
        return DASHBOARD_INTERVAL if fast else BACKGROUND_INTERVAL

    def fans_watched(self):
        return time.monotonic() - self._last_fans_poll < DASHBOARD_IDLE_AFTER

    def dashboard_polled(self, fans=False):
        """The page only polls while it is visible (not hidden, minimised or covered),
        so its polls are what keep the faster rate on. `fans`: the page showing has the
        fans on it (the Dashboard or Thermals)."""
        was_background = self.interval() == BACKGROUND_INTERVAL
        newly_fans = fans and not self.fans_watched()
        newly_watched = not self.dashboard_watched()
        self._last_poll = time.monotonic()
        if fans:
            self._last_fans_poll = self._last_poll
        if newly_watched and self._priority:
            self._priority.refresh()  # before the sampler wakes: it has to wake at full priority
        if was_background or newly_fans:
            self._wake.set()  # sample now rather than at the end of a wait

    def wake(self):
        """Sample now, e.g. as the menu opens, rather than at the end of a wait."""
        self._wake.set()

    def sample(self):
        sample = self._reader.read()
        parts = self._thermal_parts() if self._thermals else ()
        self._on_sample(sample, self._thermals.read(parts) if parts else None)

    def next_due(self, due):
        """One interval after the last sample was due, not after it ran: waits end up to
        ~90 ms late at background QoS, and that mustn't stretch the interval."""
        return max(due + self.interval(), time.monotonic() + 0.05)

    def run(self):
        import objc

        if self._priority:
            self._priority.register()
        due = time.monotonic()
        while True:
            with objc.autorelease_pool():
                try:
                    self.sample()
                except Exception:
                    log.exception("Sampling failed")
            if self._priority:
                self._priority.sampled()
            scheduled = due + self.interval()
            due = self.next_due(due)
            if self._wake.wait(due - time.monotonic()):
                due = time.monotonic()
            elif self._priority:
                # Against the schedule, not `due`: a sample that ran long makes the next wait short.
                self._priority.waited(time.monotonic() - scheduled)
            self._wake.clear()


class Api:
    """Methods callable from the dashboard page as window.pywebview.api.<name>().

    pywebview exposes every public attribute recursively, so everything that is
    not meant for JavaScript lives behind a leading underscore.
    """

    def __init__(self, app):
        self._app = app

    def get_live(self, since_seq=0, page=None):
        """`page` is the page showing; fans are read while it's one with fans on it."""
        self._app._sampler.dashboard_polled(fans=page in FAN_PAGES)
        live = self._app._live.since(int(since_seq or 0))
        # Settings' Check for Updates button follows a check through these polls.
        live["update"] = [self._app._updater.status, self._app._updater.version]
        live["fan_control"] = self._app._fans.status
        return live

    def open_fan_approval(self):
        """Login Items & Extensions, where the user allows the fan helper."""
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._app._fans.open_login_items)

    def check_for_updates(self):
        """Settings' button does what "Check for Updates…" in the menu does."""
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._app._updater.check_now)

    def get_settings(self):
        return self._app._page_settings()

    def save_settings(self, changes):
        changes = dict(changes) if isinstance(changes, dict) else {}
        if "launch_at_login" in changes:  # lives in ServiceManagement, not settings.json
            self._app._login_item.set_enabled(bool(changes.pop("launch_at_login")))
        self._app._settings.update(changes)
        return self._app._page_settings()

    def list_sessions(self):
        return self._app._store.list_sessions()

    def get_session(self, session_id):
        return self._app._store.get_session(session_id)

    def delete_session(self, session_id):
        return self._app._store.delete_session(session_id)

    def clear_history(self):
        count = self._app._store.clear()
        log.info("Deleted all charging history (%d sessions)", count)
        return count

    def export_session_csv(self, session_id):
        import webview

        session = self._app._store.get_session(session_id)
        if session is None:
            return None
        day = datetime.fromtimestamp(session["started_at"]).strftime("%Y-%m-%d-%H%M")
        result = self._app._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=f"mac-engine-room-charge-{day}.csv",
            file_types=("CSV files (*.csv)",),
        )
        if not result:
            return None
        path = result if isinstance(result, str) else result[0]
        self._app._store.export_csv(session_id, path)
        return path

    def reveal_log_file(self):
        from AppKit import NSURL, NSWorkspace
        from PyObjCTools import AppHelper

        url = NSURL.fileURLWithPath_(str(LOG_PATH))
        AppHelper.callAfter(NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_, [url])

    def open_charging_settings(self):
        """The charge limit belongs to macOS, so the page links to where it's set."""
        from AppKit import NSURL, NSWorkspace
        from PyObjCTools import AppHelper

        AppHelper.callAfter(NSWorkspace.sharedWorkspace().openURL_, NSURL.URLWithString_(CHARGING_SETTINGS_URL))

    def copy_sensor_report(self):
        """Settings' Copy button: this Mac's sensor report on the clipboard. False if the SMC
        can't be read."""
        import report
        from PyObjCTools import AppHelper

        try:
            reader = self._app._reader
            text = report.collect(VERSION, self._app._live.latest(), reader.ioreport_source, reader.ioreport_error,
                                  reader.ioreport_counters)
        except OSError as e:
            log.warning("Couldn't make the sensor report (%s)", e)
            return False
        AppHelper.callAfter(_copy_to_clipboard, text)
        log.info("Copied the sensor report")
        return True

    def report_problem(self):
        """Settings' Report a Problem…: GitHub's new-issue page with the sensor report's
        template, in the browser."""
        from AppKit import NSURL, NSWorkspace
        from launch import REPORT_ISSUE_URL
        from PyObjCTools import AppHelper

        AppHelper.callAfter(NSWorkspace.sharedWorkspace().openURL_, NSURL.URLWithString_(REPORT_ISSUE_URL))

    def app_info(self):
        import fans

        return {"version": VERSION, "log_path": str(LOG_PATH), "sensor_hint": self._app._sensor_hint(),
                "history_recording": self._app._settings.get()["record_history"],
                "update_unavailable_reason": self._app._updater.unavailable_reason,
                "fan_profiles": fans.page_profiles(), "fan_tuning_limits": fans.TUNING_LIMITS}

    def set_content_size(self, width, height, fit=False):
        """The page reports the size it needs; the window can't be made bigger.
        `fit` (page loaded or tab changed) also makes the window that tall."""
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._app._fit_window, float(width), float(height), bool(fit))

    def log_error(self, message):
        """Page errors land in app.log next to the Python ones."""
        log.warning("Dashboard error: %s", str(message)[:1000])


def _copy_to_clipboard(text):
    from AppKit import NSPasteboard, NSPasteboardTypeString

    board = NSPasteboard.generalPasteboard()
    board.clearContents()
    board.setString_forType_(text, NSPasteboardTypeString)


class EngineRoomApp:
    def __init__(self):
        import launch
        from fans import FanControl, Helper
        from history import ChargingRecorder, HistoryStore
        from power import PowerReader
        from priority import Priority
        from settings import Settings
        from thermals import ThermalsReader
        from updater import Updater

        self._settings = Settings(DATA_DIR / "settings.json", legacy_path=LEGACY_PREFS)
        self._store = HistoryStore(DATA_DIR / "history.db")
        self._live = LiveState()
        self._reader = PowerReader()
        prefs = self._settings.get()
        self._recorder = ChargingRecorder(self._store, enabled=prefs["record_history"])
        self._app_path = launch.bundle_path()
        thermals = self._thermals = ThermalsReader()
        self._priority = Priority(in_use=self._in_use, holding=lambda: self._fans.holding)
        self._fans = FanControl(self._settings, Helper(self._app_path, _own_bundle_id() or "com.pythonflow.monitor"),
                                thermals.fan_limits, priority=self._priority)
        self._sampler = Sampler(self._reader, self._on_sample, thermals, self._thermal_parts,
                                fans_running=lambda: self._fans.running, priority=self._priority)
        self._login_item = launch.LoginItem(self._app_path, DATA_DIR)
        self._updater = Updater(
            self._app_path, VERSION, settings=self._settings, data_dir=DATA_DIR,
            is_busy=self._busy_for_update, on_status=self._on_update_status, quit_app=self.quit)
        self._window = None  # the dashboard's, made the first time it opens (_make_window)
        self._html = None
        self._opening_since = None  # when the dashboard began to open, until its page has loaded for the first time
        self._page_ready = False  # the dashboard's page has loaded
        self._app_delegate = None
        self._gpu_pid = 0  # WebKit's graphics process for the page, as last seen
        self._page_size = None  # (width, height) the dashboard page last asked for
        self._statusbar = None
        self._first_reading_seen = False
        self._quitting = False
        self._wake_observer = None
        self._settings.on_change(self._on_settings_changed)

    # ── sampling (runs on the sampler thread) ──

    def _on_sample(self, sample, thermals=None):
        from PyObjCTools import AppHelper

        self._live.push(sample, thermals)
        self._recorder.feed(sample)
        self._fans.feed(thermals, sample)
        if self._statusbar is not None and not self._quitting:
            # One trip to the main thread per sample: each one wakes it. When it was read goes
            # along for the first reading's line in app.log.
            AppHelper.callAfter(self._show_sample, sample, thermals, self._settings.get(), time.monotonic())

    def _thermal_parts(self):
        """Temperatures every sample, like power, so the chart has no gaps (about 0.9 ms of
        CPU per sample on an M1 Pro). Fans only while the page or the menu shows them, or fan
        control holds them, and the safety override's sensors only while it holds them."""
        import thermals

        if self._fans.holding:
            return thermals.ALL_PARTS | {thermals.SAFETY}
        if self._sampler.fans_watched() or (self._statusbar is not None and self._statusbar.menu_open):
            return thermals.ALL_PARTS
        return thermals.TEMPERATURES

    def _sensor_hint(self):
        """Whether Settings asks for a sensor report: the app guessed CPU or GPU sensors on this
        Mac, or it's a laptop with no screen key that isn't known to have no sensor."""
        import power

        return bool(self._thermals.guessing) or power.screen_sensor_unknown(
            self._live.latest(), self._reader.has_screen_key, power.mac_model())

    def _page_settings(self):
        """Settings plus "open at login" (None where it isn't available, e.g. from source)."""
        login = self._login_item.enabled if self._login_item.available else None
        return {**self._settings.get(), "launch_at_login": login}

    def _on_settings_changed(self, values):
        from PyObjCTools import AppHelper

        self._recorder.enabled = values["record_history"]
        self._fans.nudge()  # a profile chosen or tuned: act on it now
        AppHelper.callAfter(self._apply_theme, values["theme"])

    # ── window lifecycle (main thread) ──

    def _show_sample(self, sample, thermals, settings, read_at=None):
        """`read_at`: when the sampler read it (time.monotonic())."""
        if self._quitting:
            return  # the menu bar item has gone
        self._statusbar.update(sample, thermals, settings)
        if not self._first_reading_seen:
            self._first_reading_seen = True
            age = _seconds_since_launch()
            if age is not None:
                read = age - (time.monotonic() - read_at) if read_at is not None else None
                note = first_reading_note(age, _began, read)
                if note:
                    log.info(note)
        self._check_graphics_process()

    def _in_use(self):
        """Someone is looking, or waiting to: the menu is open, or the dashboard is opening or being
        watched. Opening lasts until its page has loaded (or the window closes): its polls are what
        keep it watched, and starting WebKit for it once took 6 s on a busy Mac, twice as long as a
        poll keeps it watched."""
        return ((self._statusbar is not None and self._statusbar.menu_open) or self._opening_since is not None
                or self._sampler.dashboard_watched())

    def _on_menu_open(self):
        self._priority.refresh()  # before the sampler wakes: it has to wake at full priority
        if self._statusbar.menu_open:
            self._sampler.wake()  # the fans, which are only read while shown

    def _apply_theme(self, theme):
        import AppKit

        if self._window is None or self._window.native is None:
            return
        name = {"light": AppKit.NSAppearanceNameAqua, "dark": AppKit.NSAppearanceNameDarkAqua}.get(theme)
        self._window.native.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name) if name else None)

    def _fit_window(self, page_width=None, page_height=None, grow=False):
        """Cap the window at the size the current page needs: any wider only adds empty
        space at the sides, any taller at the bottom. Shrink it if it's bigger (e.g.
        after switching to a shorter tab). With `grow` (the page loaded, the tab
        changed, or the dashboard opened), also make it as tall as the page, as far as
        the screen allows. A window that was exactly as tall as the page keeps fitting
        it when the page grows; one dragged shorter stays that way. The top edge stays
        put unless that would push the window off the screen."""
        import AppKit

        window = self._window.native if self._window else None
        if page_width is not None:
            self._page_size = (page_width, page_height)
        if window is None or self._page_size is None:
            return
        # pywebview sets the minimum as a frame size (title bar included).
        min_size = window.contentRectForFrameRect_(((0, 0), window.minSize())).size
        max_width = max(min_size.width, self._page_size[0])
        max_height = max(min_size.height, self._page_size[1])
        was_fitting = abs(window.contentMaxSize().height - window.contentRectForFrameRect_(window.frame()).size.height) < 1
        window.setContentMaxSize_((max_width, max_height))

        content = window.contentRectForFrameRect_(window.frame())
        width = min(content.size.width, max_width)
        height = max_height if grow or was_fitting else min(content.size.height, max_height)
        if abs(width - content.size.width) < 0.5 and abs(height - content.size.height) < 0.5:
            return
        content.origin.y += content.size.height - height
        content.size = (width, height)
        frame = window.frameRectForContentRect_(content)
        screen = window.screen() or AppKit.NSScreen.mainScreen()
        if screen is not None:
            visible = screen.visibleFrame()
            if frame.size.height > visible.size.height:
                frame.origin.y += frame.size.height - visible.size.height
                frame.size.height = visible.size.height
            top = visible.origin.y + visible.size.height
            frame.origin.y = min(max(frame.origin.y, visible.origin.y), top - frame.size.height)
        # Not animated: while a window grows animated, the part it uncovers stays blank until
        # the animation ends (the Now page's charts, switching to it from Thermals).
        window.setFrame_display_animate_(frame, True, False)

    @staticmethod
    def _initial_height():
        import AppKit

        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return FIRST_PAGE_HEIGHT
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskResizable)
        fits = AppKit.NSWindow.contentRectForFrameRect_styleMask_(screen.visibleFrame(), style).size.height
        return int(min(FIRST_PAGE_HEIGHT, fits))

    def show_dashboard(self):
        """Main thread. The app is at full priority from here, the main thread's own QoS included
        (the sampler's dashboard_polled refreshes the priority), before the window and WebKit are
        made: WebKit's start goes at the main thread's QoS."""
        import AppKit

        if not self._page_ready and self._opening_since is None:
            self._opening_since = time.monotonic()  # opening until the page has loaded (see _in_use)
        self._sampler.dashboard_polled(fans=True)  # being watched from now, not from the page's first poll
        if self._window is None and not self._make_window():
            self._opening_since = None  # no window: nothing is opening
            return
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        self._apply_theme(self._settings.get()["theme"])
        self._show_tab("dashboard")
        self._window.show()
        self._fit_window(grow=True)

    def _make_window(self):
        """Makes the dashboard's window, and WebKit with it, the first time the dashboard opens
        rather than at launch: until then the app has no WebKit processes to start, keep in memory
        or wait for, and nothing stands between the menu bar item and its first reading (#59).
        Main thread, in the run loop. Returns whether there's a window now.

        It relies on pywebview's internals, checked against pywebview 6.1 by tests/test_window.py:
        start() inside a running app makes the window and returns, and the menus and app delegate
        come from BrowserView."""
        import AppKit
        import webview
        from webview.platforms import cocoa

        try:
            self._html = (_app_files_dir() / "index.html").read_text(encoding="utf-8")
            window = webview.create_window(
                APP_NAME, html=self._html, js_api=Api(self), width=WINDOW_WIDTH, height=self._initial_height(),
                min_size=MIN_WINDOW_SIZE, hidden=True, background_color=self._background_color(),
            )
            window.events.closing += self._on_closing
            window.events.loaded += self._on_loaded
            # The app is already running, so pywebview makes the window and returns.
            webview.start(private_mode=True, debug=False)
        except Exception:
            log.exception("Couldn't open the dashboard")
            return False
        self._window = window
        # pywebview sets up the menu bar's menus (Quit, Edit…) only when it starts the run loop itself.
        browser = cocoa.BrowserView.instances.get(window.uid)
        if browser is not None:
            AppKit.NSApp.setMainMenu_(browser._recreate_menus(browser.menu))
        return True

    def _on_loaded(self):
        """The page has loaded (on a thread of pywebview's)."""
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._page_loaded, time.monotonic())

    def _page_loaded(self, at):
        """Main thread: the page loaded at `at`. The first time, app.log says how long the dashboard
        took to open, making its window and starting WebKit included, and it stops counting as
        opening (`_in_use`); its polls keep it watched from here."""
        self._apply_theme(self._settings.get()["theme"])
        self._page_ready = True
        since, self._opening_since = self._opening_since, None
        if since is not None:
            log.info("Dashboard took %.1f s to open the first time", at - since)

    def _stop_opening(self):
        """Main thread: the window closed, or the app is quitting, before the page loaded. app.log
        says how long it had waited: a blank dashboard on a busy Mac was WebKit still starting."""
        since, self._opening_since = self._opening_since, None
        if since is not None:
            log.info("Dashboard hadn't loaded after %.1f s", time.monotonic() - since)

    def _show_tab(self, tab):
        """Switch the page to a tab, e.g. the Dashboard as the window opens. WebKit is asked
        directly: on the main thread, pywebview's evaluate_js would wait for itself."""
        view = self._window.native.contentView() if self._window and self._window.native else None
        if view is not None and view.respondsToSelector_("evaluateJavaScript:completionHandler:"):
            view.evaluateJavaScript_completionHandler_(f"showTab({json.dumps(tab)})", None)

    def _finish_launching(self):
        """Runs as the app finishes launching, while macOS's "open application" event is
        still current: the only moment a login launch can be told apart. The app always
        starts in the menu bar; opening it again while it runs shows the dashboard.

        Only now does the main thread join the app's priority (at full priority until then, see
        priority.py), and its own QoS follows what the app needs from here: the work AppKit hands
        off while it starts the app goes at the main thread's QoS, and at background QoS it could
        wait for a busy Mac's efficiency cores (#59)."""
        import launch

        self._priority.register(main=True)
        self._priority.started()
        log.info("Mac Engine Room %s started%s", VERSION, ", opened at login" if launch.launched_at_login() else "")

    def _check_graphics_process(self):
        """WebKit draws the page in a helper process. When that hangs, WebKit gives up on
        it but macOS doesn't let it kill the helper, so the page stays tied to it: a blank
        window until the app quits. That happened at login, with the helper stuck setting
        up audio. Finish the job, and the page reconnects to a new helper."""
        view = self._window.native.contentView() if self._window and self._window.native else None
        if view is None or not view.respondsToSelector_("_gpuProcessIdentifier"):
            return
        pid = view._gpuProcessIdentifier()
        old, self._gpu_pid = self._gpu_pid, pid
        if old and pid != old and _is_webkit_gpu_process(old):
            log.warning("WebKit's graphics process stopped responding; ending it")
            os.kill(old, signal.SIGKILL)

    def _reload_page(self):
        log.warning("WebKit's process for the dashboard page quit; reloading the page")
        self._window.load_html(self._html, "")

    def _busy_for_update(self):
        """Don't relaunch into an update while the dashboard is open or a charge is
        being recorded (a session in progress lives in memory until it ends)."""
        window = self._window.native if self._window else None
        shown = window is not None and (window.isVisible() or window.isMiniaturized())
        return shown or self._recorder.recording

    def _on_update_status(self, status, version):
        if self._statusbar is not None:
            self._statusbar.set_update_status(status, version)

    def _on_closing(self):
        """Closing the dashboard hides it; the app keeps running in the menu bar."""
        import AppKit

        if self._quitting:
            return True
        self._window.hide()
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self._stop_opening()
        self._priority.refresh()  # it isn't opening any more
        return False

    def quit(self):
        """The menu's Quit and the updater's relaunch. ⌘Q goes to `_should_terminate` directly."""
        import AppKit

        if self._quitting:
            # Already quitting. Another terminate: while AppKit waits for the answer to the first
            # one quits at once without asking the delegate (measured), before the fan thread's
            # write has finished.
            return
        log.info("Quitting")
        AppKit.NSApp.terminate_(None)

    def _should_terminate(self):
        """AppKit asks before the app quits (the menu's Quit, ⌘Q, the updater's relaunch, logging
        out). The menu bar item goes at once, and AppKit is answered later, by `_finish_quitting` on
        a thread of its own: until then AppKit runs the run loop in its modal panel mode, so the
        app stays responsive instead of beach-balling. Main thread."""
        import AppKit

        if self._quitting:
            return AppKit.NSTerminateLater  # already being answered
        self._quitting = True
        self._priority.quit()  # nothing drops to background QoS from here
        self._stop_opening()
        if self._statusbar is not None:
            self._statusbar.remove()
        threading.Thread(target=self._finish_quitting, name="pythonflow-quit", daemon=True).start()
        return AppKit.NSTerminateLater

    def _finish_quitting(self):
        """Lets a fan write in progress finish and closes the connection to the fan helper, which
        hands the fans back to macOS as it closes, then tells AppKit to go ahead. On a busy Mac,
        waiting for the helper's answer to `auto` on the main thread took 4.4 s at Quit."""
        import AppKit

        import priority

        priority.set_own_qos(priority.QOS_CLASS_USER_INTERACTIVE)  # someone is waiting for the app to go
        try:
            self._fans.stop()
        except Exception:
            log.exception("Fan control didn't stop cleanly")
        finally:
            # call_on_main posts in the run loop's common modes, which include the modal panel mode
            # AppKit waits in (AppHelper.callLater's delayed perform doesn't run there).
            priority.call_on_main(lambda: AppKit.NSApp.replyToApplicationShouldTerminate_(True))

    def _install_delegate_methods(self):
        """Add to pywebview's delegates.

        ⌘Q in the dashboard quits the app instead of just hiding the window. pywebview's
        app delegate asks each window's `closing` handlers before terminating, and ours
        cancels closes (to hide the window instead), so a real quit request must win.

        Opening Mac Engine Room again while it runs in the background (from Finder, Spotlight
        or Launchpad) shows the dashboard. Otherwise nothing would seem to happen.

        If WebKit's process for the page quits (a crash, or macOS reclaiming memory),
        the page is loaded again instead of staying blank.

        pywebview only sets an app delegate as it makes the window, which waits for the
        dashboard to open, so one is set here: quitting (the fans go back to macOS as the app goes)
        and opening the app again go through it before then.
        """
        import AppKit
        import objc
        from PyObjCTools import AppHelper
        from webview.platforms import cocoa

        app = self

        def applicationShouldTerminate_(self, sender):
            return app._should_terminate()  # the fans go back to macOS as the app goes

        def reopen(self, sender, has_visible_windows):
            if has_visible_windows:
                return True  # AppKit brings the dashboard forward
            app.show_dashboard()
            return False

        def web_process_quit(self, webview):
            AppHelper.callAfter(app._reload_page)

        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [
            applicationShouldTerminate_,
            objc.selector(reopen, selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z"),
        ])
        objc.classAddMethods(cocoa.BrowserView.BrowserDelegate, [
            objc.selector(web_process_quit, selector=b"webViewWebContentProcessDidTerminate:", signature=b"v@:@"),
        ])
        self._app_delegate = cocoa.BrowserView.AppDelegate.alloc().init()  # kept: NSApp doesn't retain it
        AppKit.NSApp.setDelegate_(self._app_delegate)

    def _background_color(self):
        import AppKit

        theme = self._settings.get()["theme"]
        if theme == "system":
            best = AppKit.NSApp.effectiveAppearance().bestMatchFromAppearancesWithNames_(
                [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
            theme = "dark" if best == AppKit.NSAppearanceNameDarkAqua else "light"
        return "#1c1c1e" if theme == "dark" else "#f5f5f7"

    def _import_legacy_history(self):
        try:
            self._store.import_legacy(LEGACY_DB)
        except Exception:
            log.exception("Importing history from the previous version failed")

    def run(self):
        import AppKit
        import webview.platforms.cocoa  # noqa: F401 — sets up NSApp so the menu bar item can exist early
        from PyObjCTools import AppHelper

        import launch
        import rename
        from statusbar import StatusBar

        if self._app_path and not launch.in_applications_folder(self._app_path):
            if launch.offer_to_move(self._app_path):
                return  # the copy in Applications starts once this one has quit
        elif self._app_path:
            if rename.rename_bundle(self._app_path, self._login_item):
                return  # the renamed copy starts once this one has quit
            if rename.trash_old_copies(self._app_path):
                self._login_item.repoint()
        if rename.LOGIN_ITEM_ARGUMENT in sys.argv:
            self._login_item.set_enabled(True)

        # The app starts in the menu bar. The menu or opening the app again shows the dashboard,
        # whose window is made then (`_make_window`).
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self._install_delegate_methods()

        self._statusbar = StatusBar(
            on_open=self.show_dashboard, on_check_updates=self._updater.check_now, on_quit=self.quit,
            on_menu_open=self._on_menu_open, fan_status=lambda: self._fans.status,
            on_profile=lambda pid: self._settings.update({"fan_profile": pid}),
            on_allow_fans=self._fans.open_login_items)
        self._sampler.start()
        self._fans.start()
        self._wake_observer = AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidWakeNotification, None, None, lambda notification: self._fans.did_wake())
        threading.Thread(target=self._import_legacy_history, name="legacy-import", daemon=True).start()
        self._login_item.turn_on_by_default()
        self._updater.start()
        # The main thread registers with the app's priority in _finish_launching.

        if AppKit.NSRunningApplication.currentApplication().isFinishedLaunching():  # e.g. after offer_to_move
            AppHelper.callAfter(self._finish_launching)
        else:
            AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                AppKit.NSApplicationDidFinishLaunchingNotification, None, None,
                lambda notification: self._finish_launching())
        # The app runs itself rather than through pywebview's start(), which makes the window first.
        AppHelper.installMachInterrupt()
        AppKit.NSApp.run()
        os._exit(0)


def _own_bundle_id():
    if not getattr(sys, "frozen", False):
        return None
    from Foundation import NSBundle

    return NSBundle.mainBundle().bundleIdentifier()


def main():
    global _began
    import rename

    _began = _seconds_since_launch()
    # Before logging and the lock, which live in the folders being moved.
    moved = rename.move_old_folders(OLD_FOLDERS, _own_bundle_id())
    if moved is None:
        print(f"{OLD_NAME} is still running from {OLD_FOLDERS[0][0]}; exiting", file=sys.stderr)
        return
    _setup_logging()
    for note in moved:
        log.info(note)
    lock = _acquire_single_instance_lock()
    if lock is None:
        log.info("Another Mac Engine Room instance is already running; exiting")
        return
    sys.path.insert(0, str(_app_files_dir()))
    try:
        EngineRoomApp().run()
    except Exception:
        log.exception("Mac Engine Room failed to start")
        raise


if __name__ == "__main__":
    main()
