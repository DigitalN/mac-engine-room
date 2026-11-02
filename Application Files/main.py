"""Mac Engine Room (formerly Pythonflow) — a menu bar thermals and power monitor for macOS.

Launcher: logging, single-instance lock, sampler thread, menu bar item and the
pywebview dashboard window.

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
VERSION = "1.2.7"  # the build reads it from here; see the release checklist in docs/DEVELOPMENT.md
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
FIRST_PAGE_HEIGHT = 411
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


# <sys/qos.h>. On Apple silicon only background QoS keeps a thread on the efficiency
# cores; utility and above use the performance cores when they're free. The cost is
# waiting: timed waits end ~90 ms late at background QoS (~50 ms at utility), so they
# coalesce with other wakeups, and with every core busy a background thread waited up
# to 1.6 s for a turn, against 0.1 s at utility (measured on an M1 Pro).
QOS_CLASS_USER_INTERACTIVE = 0x21
QOS_CLASS_UTILITY = 0x11
QOS_CLASS_BACKGROUND = 0x09


def _set_thread_qos(qos_class):
    """Set the calling thread's QoS. Only a thread can change its own."""
    err = ctypes.CDLL("/usr/lib/libSystem.B.dylib").pthread_set_qos_class_self_np(qos_class, 0)
    if err:
        log.warning("Couldn't set thread QoS %#x (error %d)", qos_class, err)


def _fans_qos(holding):
    """Fan control's thread: on the efficiency cores while the fans are with macOS, utility
    while it holds them, so its check-ins reach the helper even on a busy Mac."""
    _set_thread_qos(QOS_CLASS_UTILITY if holding else QOS_CLASS_BACKGROUND)


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
    the curve needs a reading every second; `fans_held`: it holds the fans."""

    def __init__(self, reader, on_sample, thermals=None, thermal_parts=lambda: (),
                 fans_running=lambda: False, fans_held=lambda: False):
        super().__init__(name="pythonflow-sampler", daemon=True)
        self._reader = reader
        self._on_sample = on_sample
        self._thermals = thermals
        self._thermal_parts = thermal_parts
        self._fans_running = fans_running
        self._fans_held = fans_held
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
        self._last_poll = time.monotonic()
        if fans:
            self._last_fans_poll = self._last_poll
        if was_background or newly_fans:
            self._wake.set()  # sample now rather than at the end of a wait

    def wake(self):
        """Sample now, e.g. as the menu opens, rather than at the end of a wait."""
        self._wake.set()

    def sample(self):
        sample = self._reader.read()
        parts = self._thermal_parts() if self._thermals else ()
        self._on_sample(sample, self._thermals.read(parts) if parts else None)

    def qos(self):
        """Background QoS, on the efficiency cores, even with a fan profile watching the
        temperatures. While the dashboard is watched or the fans are held, utility, so a busy
        Mac can't stall the chart or the fans for seconds."""
        busy = self.dashboard_watched() or self._fans_held()
        return QOS_CLASS_UTILITY if busy else QOS_CLASS_BACKGROUND

    def next_due(self, due):
        """One interval after the last sample was due, not after it ran: waits end up to
        ~90 ms late at background QoS, and that mustn't stretch the interval."""
        return max(due + self.interval(), time.monotonic() + 0.05)

    def run(self):
        import objc

        qos = None
        due = time.monotonic()
        while True:
            if self.qos() != qos:
                qos = self.qos()
                _set_thread_qos(qos)
            with objc.autorelease_pool():
                try:
                    self.sample()
                except Exception:
                    log.exception("Sampling failed")
            due = self.next_due(due)
            if self._wake.wait(due - time.monotonic()):
                due = time.monotonic()
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

    def app_info(self):
        import fans

        return {"version": VERSION, "log_path": str(LOG_PATH),
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


class EngineRoomApp:
    def __init__(self):
        import launch
        from fans import FanControl, Helper
        from history import ChargingRecorder, HistoryStore
        from power import PowerReader
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
        thermals = ThermalsReader()
        self._fans = FanControl(self._settings, Helper(self._app_path, _own_bundle_id() or "com.pythonflow.monitor"),
                                thermals.fan_limits, set_qos=_fans_qos)
        self._sampler = Sampler(self._reader, self._on_sample, thermals, self._thermal_parts,
                                fans_running=lambda: self._fans.running, fans_held=lambda: self._fans.holding)
        self._login_item = launch.LoginItem(self._app_path, DATA_DIR)
        self._updater = Updater(
            self._app_path, VERSION, settings=self._settings, data_dir=DATA_DIR,
            is_busy=self._busy_for_update, on_status=self._on_update_status, quit_app=self.quit)
        self._window = None
        self._html = None
        self._gpu_pid = 0  # WebKit's graphics process for the page, as last seen
        self._page_size = None  # (width, height) the dashboard page last asked for
        self._statusbar = None
        self._dashboard_shown = False
        self._main_qos = None
        self._quitting = False
        self._wake_observer = None
        self._settings.on_change(self._on_settings_changed)

    # ── sampling (runs on the sampler thread) ──

    def _on_sample(self, sample, thermals=None):
        from PyObjCTools import AppHelper

        self._live.push(sample, thermals)
        self._recorder.feed(sample)
        self._fans.feed(thermals, sample)
        if self._statusbar is not None:
            # One trip to the main thread per sample: each one wakes it.
            AppHelper.callAfter(self._show_sample, sample, thermals, self._settings.get())

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

    def _page_settings(self):
        """Settings plus "open at login" (None where it isn't available, e.g. from source)."""
        login = self._login_item.enabled if self._login_item.available else None
        return {**self._settings.get(), "launch_at_login": login}

    def _on_settings_changed(self, values):
        from PyObjCTools import AppHelper

        self._recorder.enabled = values["record_history"]
        self._fans.nudge()  # a profile chosen, or Manual's slider moved: act on it now
        AppHelper.callAfter(self._apply_theme, values["theme"])

    # ── window lifecycle (main thread) ──

    def _show_sample(self, sample, thermals, settings):
        self._statusbar.update(sample, thermals, settings)
        self._check_graphics_process()

    def _on_menu_open(self):
        self._update_main_qos()
        if self._statusbar.menu_open:
            self._sampler.wake()  # the fans, which are only read while shown

    def _update_main_qos(self):
        """With only the menu bar item showing, the main thread (which redraws it) runs
        at background QoS, on the efficiency cores. With the dashboard or the menu open,
        it's back at user-interactive."""
        in_use = self._dashboard_shown or (self._statusbar is not None and self._statusbar.menu_open)
        qos = QOS_CLASS_USER_INTERACTIVE if in_use else QOS_CLASS_BACKGROUND
        if qos != self._main_qos:
            _set_thread_qos(qos)
            self._main_qos = qos

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
        import AppKit

        self._dashboard_shown = True
        self._update_main_qos()
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        self._apply_theme(self._settings.get()["theme"])
        self._show_tab("dashboard")
        self._window.show()
        self._fit_window(grow=True)

    def _show_tab(self, tab):
        """Switch the page to a tab, e.g. the Dashboard as the window opens. WebKit is asked
        directly: on the main thread, pywebview's evaluate_js would wait for itself."""
        view = self._window.native.contentView() if self._window and self._window.native else None
        if view is not None and view.respondsToSelector_("evaluateJavaScript:completionHandler:"):
            view.evaluateJavaScript_completionHandler_(f"showTab({json.dumps(tab)})", None)

    def _finish_launching(self, show):
        """Runs as the app finishes launching, while macOS's "open application" event is
        still current: the only moment a login launch can be told apart. At login the
        dashboard stays hidden, whatever the setting says."""
        import launch

        at_login = launch.launched_at_login()
        if show and not at_login:
            self.show_dashboard()
        else:
            self._update_main_qos()
        log.info("Mac Engine Room %s started (dashboard %s%s)", VERSION,
                 "shown" if show and not at_login else "hidden", ", opened at login" if at_login else "")

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
        self._dashboard_shown = False
        self._update_main_qos()
        return False

    def quit(self):
        import AppKit

        log.info("Quitting")
        self._quitting = True
        AppKit.NSApp.terminate_(None)

    def _install_delegate_methods(self):
        """Add to pywebview's delegates.

        ⌘Q in the dashboard quits the app instead of just hiding the window. pywebview's
        app delegate asks each window's `closing` handlers before terminating, and ours
        cancels closes (to hide the window instead), so a real quit request must win.

        Opening Mac Engine Room again while it runs in the background (from Finder, Spotlight
        or Launchpad) shows the dashboard. Otherwise nothing would seem to happen.

        If WebKit's process for the page quits (a crash, or macOS reclaiming memory),
        the page is loaded again instead of staying blank.
        """
        import AppKit
        import objc
        from PyObjCTools import AppHelper
        from webview.platforms import cocoa

        app = self

        def applicationShouldTerminate_(self, sender):
            app._quitting = True
            app._fans.stop()  # the fans go back to macOS before the app goes
            return AppKit.NSTerminateNow

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
        import webview
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

        # The window starts hidden, and _finish_launching shows it unless this is a login
        # launch. Relaunching into an update shouldn't pop the dashboard up either.
        show = self._settings.get()["open_dashboard_at_launch"] and launch.BACKGROUND_ARGUMENT not in sys.argv
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self._install_delegate_methods()

        self._html = (_app_files_dir() / "index.html").read_text(encoding="utf-8")
        self._window = webview.create_window(
            APP_NAME, html=self._html, js_api=Api(self), width=WINDOW_WIDTH, height=self._initial_height(),
            min_size=MIN_WINDOW_SIZE, hidden=True, background_color=self._background_color(),
        )
        self._window.events.closing += self._on_closing
        # Non-locking pywebview events fire on a worker thread; AppKit needs the main one.
        self._window.events.loaded += lambda: AppHelper.callAfter(
            self._apply_theme, self._settings.get()["theme"])

        self._statusbar = StatusBar(
            on_open=self.show_dashboard, on_check_updates=self._updater.check_now, on_quit=self.quit,
            on_menu_open=self._on_menu_open, fan_status=lambda: self._fans.status,
            on_profile=lambda pid: self._settings.update({"fan_profile": pid}),
            on_manual_level=lambda level: self._settings.update({"fan_manual_level": level}),
            on_allow_fans=self._fans.open_login_items)
        self._sampler.start()
        self._fans.start()
        self._wake_observer = AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidWakeNotification, None, None, lambda notification: self._fans.did_wake())
        threading.Thread(target=self._import_legacy_history, name="legacy-import", daemon=True).start()
        self._login_item.turn_on_by_default()
        self._updater.start()

        if AppKit.NSRunningApplication.currentApplication().isFinishedLaunching():  # e.g. after offer_to_move
            AppHelper.callAfter(self._finish_launching, show)
        else:
            AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                AppKit.NSApplicationDidFinishLaunchingNotification, None, None,
                lambda notification: self._finish_launching(show))
        webview.start(private_mode=True, debug=False)
        os._exit(0)


def _own_bundle_id():
    if not getattr(sys, "frozen", False):
        return None
    from Foundation import NSBundle

    return NSBundle.mainBundle().bundleIdentifier()


def main():
    import rename

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
