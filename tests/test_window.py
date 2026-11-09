"""The dashboard's window, made the first time the dashboard opens, and what happens as the app
starts (the app is a stand-in: no window, WebKit or menu bar item is made). Also the parts of
pywebview's code that making the window inside the running app relies on."""

import ast
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import AppKit
from PyObjCTools import AppHelper

import main
from main import EngineRoomApp


def stand_in_app(**fields):
    return SimpleNamespace(
        _window=None, _sampler=SimpleNamespace(dashboard_polled=lambda fans: None),
        _settings=SimpleNamespace(get=lambda: {"theme": "dark"}), _apply_theme=lambda theme: None,
        _show_tab=lambda tab: None, _fit_window=lambda grow: None, **fields)


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
    app = stand_in_app(_opening_since=100.0)
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        EngineRoomApp._on_loaded(app)
        EngineRoomApp._on_loaded(app)  # loaded again after WebKit's process for the page quit: nothing to say
    assert [r.getMessage() for r in caplog.records] == ["Dashboard took 0.8 s to open the first time"]
    assert calls == [(app._apply_theme, ("dark",))] * 2  # the theme, on the main thread, each time


def test_the_main_thread_joins_the_app_priority_once_the_app_has_started(caplog):
    """Before then it keeps its own QoS, so AppKit's start-up work isn't handed off at background
    QoS (#59)."""
    seen = []
    app = SimpleNamespace(_priority=SimpleNamespace(register=lambda: seen.append("register"),
                                                    started=lambda: seen.append("started")))
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        EngineRoomApp._finish_launching(app)
    assert seen == ["register", "started"]
    assert [r.getMessage() for r in caplog.records] == [f"Mac Engine Room {main.VERSION} started"]


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
