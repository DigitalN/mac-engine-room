"""The dashboard's window, made the first time the dashboard opens, and what happens as the app
starts (the app is a stand-in: no window, WebKit or menu bar item is made). Also the parts of
pywebview's code that making the window inside the running app relies on."""

import ast
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import AppKit
import pytest
from PyObjCTools import AppHelper

import main
import priority
from main import EngineRoomApp, Sampler
from priority import QOS_CLASS_BACKGROUND, QOS_CLASS_USER_INTERACTIVE, QUIET, CpuIdle, Priority


def stand_in_app(**fields):
    app = SimpleNamespace(
        _window=None, _sampler=SimpleNamespace(dashboard_polled=lambda fans: None),
        _settings=SimpleNamespace(get=lambda: {"theme": "dark"}), _apply_theme=lambda theme: None,
        _show_tab=lambda tab: None, _fit_window=lambda grow: None, _page_ready=False, _opening_since=None,
        _quitting=False, _statusbar=None)
    for name, value in fields.items():
        setattr(app, name, value)
    return app


def test_the_window_is_made_the_first_time_the_dashboard_opens_and_kept(monkeypatch):
    policies = []
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(setActivationPolicy_=policies.append))
    shown, made = [], []
    app = stand_in_app()
    window = SimpleNamespace(show=lambda: shown.append(1))

    def make_window():
        made.append(1)
        app._window = window
        return True

    app._make_window = make_window
    for _ in range(3):
        EngineRoomApp.show_dashboard(app)
    assert made == [1] and len(shown) == 3
    assert policies == [AppKit.NSApplicationActivationPolicyRegular] * 3


def test_a_window_that_could_not_be_made_shows_nothing(monkeypatch):
    policies = []
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(setActivationPolicy_=policies.append))
    app = stand_in_app(_make_window=lambda: False)
    EngineRoomApp.show_dashboard(app)
    assert policies == []  # no Dock icon or menus for a window that isn't there


def test_the_first_page_load_says_how_long_the_dashboard_took_to_open(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *args: calls.append((func, args)))
    monkeypatch.setattr(main.time, "monotonic", lambda: 100.8)
    themes = []
    app = stand_in_app(_opening_since=100.0, _apply_theme=themes.append)
    app._page_loaded = lambda at: EngineRoomApp._page_loaded(app, at)
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        EngineRoomApp._on_loaded(app)
        EngineRoomApp._on_loaded(app)  # loaded again after WebKit's process for the page quit: nothing to say
        assert calls == [(app._page_loaded, (100.8,))] * 2  # on the main thread, with when it loaded
        for func, args in calls:
            func(*args)
    assert [r.getMessage() for r in caplog.records] == ["Dashboard took 0.8 s to open the first time"]
    assert themes == ["dark"] * 2  # the theme, each time
    assert app._page_ready and app._opening_since is None


def test_the_main_thread_joins_the_app_priority_once_the_app_has_started(caplog):
    """Before then it keeps its own QoS, so AppKit's start-up work isn't handed off at background
    QoS (#59). It registers as the main thread, whose own QoS follows what the app needs."""
    seen = []
    app = SimpleNamespace(_priority=SimpleNamespace(register=lambda main=False: seen.append(("register", main)),
                                                    started=lambda: seen.append("started")))
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        EngineRoomApp._finish_launching(app)
    assert seen == [("register", True), "started"]
    assert [r.getMessage() for r in caplog.records] == [f"Mac Engine Room {main.VERSION} started"]


# ── opening the dashboard on a busy Mac ──

def priority_on_this_thread(app, qos):
    """A real Priority whose main thread is this one, with stand-ins for the system calls, deciding
    from the app's own _in_use, on a quiet Mac with nothing open."""
    p = Priority(in_use=lambda: EngineRoomApp._in_use(app), idle=CpuIdle(read=lambda: None, cores=()),
                 set_qos=qos.append, raise_=lambda ident: 1, lower=lambda handle: None,
                 on_main=lambda func: pytest.fail("sent to the main thread from the main thread"))
    p.register(main=True)
    p.started()
    p.load.state = QUIET
    p.refresh()
    return p


def test_the_main_thread_is_at_user_interactive_qos_before_the_window_and_webkit_are_made(monkeypatch):
    """WebKit's start goes at the main thread's own QoS, whatever its override: on a Mac with every
    core busy the page took 0.4 to 18.6 s to load at background QoS with the override, 0.32 to 0.35 s
    at user-interactive."""
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(setActivationPolicy_=lambda policy: None))
    qos, seen = [], []
    app = stand_in_app()
    app._sampler = Sampler(reader=None, on_sample=None)
    app._priority = app._sampler._priority = priority_on_this_thread(app, qos)
    assert qos[-1] == QOS_CLASS_BACKGROUND

    def make_window():
        seen.append(qos[-1])
        app._window = SimpleNamespace(show=lambda: None)
        return True

    app._make_window = make_window
    EngineRoomApp.show_dashboard(app)
    assert seen == [QOS_CLASS_USER_INTERACTIVE]


def test_the_dashboard_counts_as_in_use_until_its_page_has_loaded_however_long_that_takes(monkeypatch):
    """Its polls keep it watched for 3 s; starting WebKit took 6 s once on a busy Mac."""
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(setActivationPolicy_=lambda policy: None))
    app = stand_in_app(_sampler=Sampler(reader=None, on_sample=None), _make_window=lambda: False)
    EngineRoomApp.show_dashboard(app)  # the window couldn't be made: not opening
    assert app._opening_since is None
    app._make_window = lambda: setattr(app, "_window", SimpleNamespace(show=lambda: None)) or True
    EngineRoomApp.show_dashboard(app)
    now[0] += 10.0  # long past the 3 s a poll keeps it watched, and still loading
    assert not app._sampler.dashboard_watched() and EngineRoomApp._in_use(app)
    EngineRoomApp._page_loaded(app, now[0])
    assert not EngineRoomApp._in_use(app)
    EngineRoomApp.show_dashboard(app)  # opened again once loaded: watched, not opening
    assert app._opening_since is None


def test_closing_the_window_before_its_page_loaded_says_so_and_lets_the_priority_drop(monkeypatch, caplog):
    policies, refreshed = [], []
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(setActivationPolicy_=policies.append))
    monkeypatch.setattr(main.time, "monotonic", lambda: 106.04)
    app = stand_in_app(_opening_since=100.0, _window=SimpleNamespace(hide=lambda: None))
    app._stop_opening = lambda: EngineRoomApp._stop_opening(app)
    app._priority = SimpleNamespace(refresh=lambda: refreshed.append(app._opening_since))
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        assert EngineRoomApp._on_closing(app) is False  # hidden, not closed
    assert [r.getMessage() for r in caplog.records] == ["Dashboard hadn't loaded after 6.0 s"]
    assert refreshed == [None]  # after it stopped counting as opening
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        EngineRoomApp._on_closing(app)  # closed again: nothing more to say
    assert caplog.records == []


def test_a_window_opened_again_before_its_page_loaded_is_opening_again(monkeypatch):
    monkeypatch.setattr(main.time, "monotonic", lambda: 200.0)
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(setActivationPolicy_=lambda policy: None))
    app = stand_in_app(_window=SimpleNamespace(show=lambda: None))  # made, its page never loaded
    EngineRoomApp.show_dashboard(app)
    assert app._opening_since == 200.0


# ── quitting ──

class _Thread:
    started = []

    def __init__(self, target, name, daemon):
        self.target = target

    def start(self):
        _Thread.started.append(self.target)


def quitting_app(monkeypatch, order):
    monkeypatch.setattr(main.time, "monotonic", lambda: 107.5)
    monkeypatch.setattr(main.threading, "Thread", _Thread)
    _Thread.started = []
    app = stand_in_app(_opening_since=100.0,
                       _priority=SimpleNamespace(quit=lambda: order.append("priority quits")),
                       _statusbar=SimpleNamespace(remove=lambda: order.append("menu bar item removed")),
                       _fans=SimpleNamespace(stop=lambda: order.append("fans stop")))
    app._stop_opening = lambda: EngineRoomApp._stop_opening(app)
    app._finish_quitting = lambda: EngineRoomApp._finish_quitting(app)
    return app


def test_quitting_removes_the_menu_bar_item_at_once_and_answers_appkit_later(monkeypatch, caplog):
    """Waiting on the main thread for the fans to be handed back beach-balled Quit for 4.4 s on a busy
    Mac, and then 4.6 s more at background QoS."""
    order = []
    app = quitting_app(monkeypatch, order)
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        assert EngineRoomApp._should_terminate(app) == AppKit.NSTerminateLater
    assert app._quitting and order == ["priority quits", "menu bar item removed"]  # nothing waited for yet
    assert _Thread.started == [app._finish_quitting]
    assert [r.getMessage() for r in caplog.records] == ["Dashboard hadn't loaded after 7.5 s"]
    assert EngineRoomApp._should_terminate(app) == AppKit.NSTerminateLater  # asked again: one answer, one thread
    assert len(_Thread.started) == 1


def test_the_quit_thread_stops_fan_control_then_tells_appkit_on_the_main_thread(monkeypatch):
    order = []
    app = quitting_app(monkeypatch, order)
    monkeypatch.setattr(priority, "set_own_qos", lambda qos: order.append(("qos", qos)))
    monkeypatch.setattr(priority, "call_on_main", lambda func: order.append(("main", func)))
    replies = []
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(replyToApplicationShouldTerminate_=replies.append))
    EngineRoomApp._should_terminate(app)
    _Thread.started[0]()
    assert order[2:4] == [("qos", QOS_CLASS_USER_INTERACTIVE), "fans stop"]
    assert order[4][0] == "main" and replies == []
    order[4][1]()  # on the main thread
    assert replies == [True]


def test_appkit_is_told_to_go_ahead_even_if_fan_control_fails_to_stop(monkeypatch, caplog):
    order = []
    app = quitting_app(monkeypatch, order)
    app._fans = SimpleNamespace(stop=lambda: 1 / 0)
    monkeypatch.setattr(priority, "set_own_qos", lambda qos: None)
    monkeypatch.setattr(priority, "call_on_main", lambda func: order.append("told"))
    with caplog.at_level(logging.ERROR, logger="pythonflow"):
        EngineRoomApp._finish_quitting(app)
    assert order == ["told"] and "Fan control didn't stop cleanly" in caplog.text


def test_quit_asks_appkit_once(monkeypatch):
    """A second terminate: while AppKit waits for the first one's answer quits at once, without
    asking the delegate (measured)."""
    asked = []
    monkeypatch.setattr(AppKit, "NSApp", SimpleNamespace(terminate_=asked.append))
    app = stand_in_app()
    EngineRoomApp.quit(app)
    app._quitting = True  # what AppKit's answer from _should_terminate did
    EngineRoomApp.quit(app)
    assert asked == [None]


def test_readings_stop_reaching_the_menu_bar_once_the_app_is_quitting(monkeypatch):
    posted, shown = [], []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *args: posted.append(func))
    app = stand_in_app(_quitting=True, _live=SimpleNamespace(push=lambda s, t: None),
                       _recorder=SimpleNamespace(feed=lambda s: None), _fans=SimpleNamespace(feed=lambda t, s: None),
                       _statusbar=SimpleNamespace(update=lambda *reading: shown.append(reading)))
    EngineRoomApp._on_sample(app, {"t": 1})
    EngineRoomApp._show_sample(app, {"t": 1}, None, {})  # one sent before the quit began
    assert posted == [] and shown == []


QUIT_LATER = """
import sys, threading
sys.path.insert(0, sys.argv[1])
import AppKit
from Foundation import NSObject, NSRunLoop
import priority

class Delegate(NSObject):
    def applicationShouldTerminate_(self, sender):
        def answer():
            print("answered in", NSRunLoop.currentRunLoop().currentMode(), flush=True)
            AppKit.NSApp.replyToApplicationShouldTerminate_(True)
        threading.Thread(target=lambda: priority.call_on_main(answer)).start()
        return AppKit.NSTerminateLater

    def applicationWillTerminate_(self, notification):
        print("terminating", flush=True)

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon, no window
delegate = Delegate.alloc().init()
app.setDelegate_(delegate)
priority.call_on_main(lambda: app.terminate_(None))
threading.Timer(10, lambda: (print("still running", flush=True), __import__("os")._exit(3))).start()
app.run()
"""


def test_the_answer_to_appkit_reaches_the_main_thread_while_it_waits_to_quit():
    """_finish_quitting answers through priority.call_on_main (AppHelper.callAfter), which posts in
    the run loop's common modes. AppKit waits for the answer in its modal panel mode, where a
    default-mode perform (AppHelper.callLater) never runs, and the app would never quit. In a
    process of its own: an app with no window or Dock icon."""
    import subprocess
    import sys

    folder = str(Path(main.__file__).resolve().parent)
    run = subprocess.run([sys.executable, "-I", "-c", QUIT_LATER, folder], capture_output=True, text=True, timeout=30)
    assert run.stdout.split("\n")[:2] == ["answered in NSModalPanelRunLoopMode", "terminating"], run.stdout + run.stderr
    assert run.returncode == 0


# ── pywebview's code that _make_window relies on (checked against pywebview 6.1) ──
# requirements.txt pins 6.1, but the build bundles whichever pywebview is installed. These read
# pywebview's source rather than importing its Cocoa backend: importing that makes the test
# process an app, with a Dock icon.

def pywebview_source(module):
    """The source of one of pywebview's modules and its parsed tree, without importing it."""
    source = Path(importlib.util.find_spec(module).origin).read_text(encoding="utf-8")
    return source, ast.parse(source)


def checks_not_running(test):
    """Whether an `if` test is `not ….isRunning()`."""
    return (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Call)
            and isinstance(test.operand.func, ast.Attribute) and test.operand.func.attr == "isRunning")


def run_loop_calls(node, guarded=False):
    """(guarded, line) for each `….run()` call under `node`: guarded when it's in the body of an
    `if not ….isRunning():`."""
    if isinstance(node, ast.If):
        for child in node.body:
            yield from run_loop_calls(child, guarded or checks_not_running(node.test))
        for child in (node.test, *node.orelse):
            yield from run_loop_calls(child, guarded)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run":
        yield guarded, node.lineno
    for child in ast.iter_child_nodes(node):
        yield from run_loop_calls(child, guarded)


def test_webview_start_returns_when_the_app_is_already_running():
    """_make_window calls webview.start() inside the running app (pywebview 6.1). It returns only
    because pywebview makes the first window there and then, starts its run loop only for an app
    that isn't running, and does nothing after making the window. A pywebview that ran the loop
    anyway would sit in a nested run loop inside show_dashboard; one that queued the window would
    leave Window.show() waiting 20 s for it on the main thread."""
    source, tree = pywebview_source("webview.platforms.cocoa")
    runs = list(run_loop_calls(tree))
    assert runs and all(guarded for guarded, _ in runs), f"a run loop started without checking isRunning(): {runs}"
    browser_view = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BrowserView")
    first_show = next(n for n in browser_view.body if isinstance(n, ast.FunctionDef) and n.name == "first_show")
    assert any(guarded for guarded, _ in run_loop_calls(first_show))

    create_window = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_window")
    master = [n for n in ast.walk(create_window)
              if isinstance(n, ast.If) and "'master'" in ast.get_source_segment(source, n.test)]
    assert master and any(isinstance(call, ast.Call) and getattr(call.func, "id", None) == "create"
                          for statement in master[0].body for call in ast.walk(statement))

    source, tree = pywebview_source("webview")
    start = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start")
    made = next(i for i, statement in enumerate(start.body)
                if "guilib.create_window(windows[0])" in ast.get_source_segment(source, statement))
    after = [ast.get_source_segment(source, call.func)
             for statement in start.body[made + 1:] for call in ast.walk(statement) if isinstance(call, ast.Call)]
    assert after == ["os.unlink"]  # only the SSL certificate's cleanup


def test_the_menus_and_app_delegate_the_app_takes_from_pywebview_are_still_there():
    """pywebview 6.1: the menu bar's menus come from BrowserView._recreate_menus, and pywebview makes
    its app delegate from BrowserView.AppDelegate, the class the app adds its quit (fans back to
    macOS) and reopen methods to and makes its own delegate from at launch."""
    source, tree = pywebview_source("webview.platforms.cocoa")
    browser_view = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BrowserView")
    members = {n.name: n for n in browser_view.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert isinstance(members.get("_recreate_menus"), ast.FunctionDef)
    assert isinstance(members.get("AppDelegate"), ast.ClassDef)
    init = ast.get_source_segment(source, members["__init__"])
    assert "BrowserView.AppDelegate.alloc()" in init and "setDelegate_(self._appDelegate)" in init
