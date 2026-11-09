"""The dashboard's window, made the first time the dashboard opens, and what happens as the app
starts (the app is a stand-in: no window, WebKit or menu bar item is made)."""

import logging
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
