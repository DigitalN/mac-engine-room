"""Mouse (mouse.py) with a stand-in for libmouse.dylib and for Accessibility: when the tap is made and
dropped, when macOS is asked, the updater's stop and start, and what app.log says. The dylib itself is
in test_mouse_native.py."""

import logging

import accessibility
import mouse
from mouse import NOT_ALLOWED, OFF, ON, Mouse, describe, should_ask, switches
from settings import DEFAULTS, validate


class FakeLibrary:
    """libmouse.dylib: makes its tap only while a switch is on and Accessibility is allowed."""

    def __init__(self, access):
        self._access = access
        self.tap = None  # the switches the tap runs with, or None
        self.calls = []

    def configure(self, *on):
        self.calls.append(("configure", on))
        if not any(on):
            self.tap = None
            return OFF
        if self.tap is None and not self._access.is_allowed:
            return NOT_ALLOWED
        self.tap = on
        return ON

    def stop(self):
        self.calls.append(("stop",))
        self.tap = None

    def running(self):
        return self.tap is not None


class FakeAccess:
    def __init__(self, allowed=False):
        self.is_allowed = allowed
        self.asked = 0
        self.watchers = []
        self.taps = []

    def allowed(self):
        return self.is_allowed

    def ask(self):
        self.asked += 1
        return self.is_allowed

    def watch(self, callback):
        self.watchers.append(callback)

    def register(self, stop, start):
        self.taps.append((stop, start))

    def grant(self, allowed):
        """macOS's list changed: the watchers hear about it."""
        self.is_allowed = allowed
        for callback in self.watchers:
            callback(allowed)


class FakeSettings:
    def __init__(self, **values):
        self._values = validate({**DEFAULTS, **values})

    def get(self):
        return dict(self._values)


def make(allowed=False, **values):
    access = FakeAccess(allowed)
    library = FakeLibrary(access)
    m = Mouse(FakeSettings(**values), library=library, access=access, call_on_main=lambda fn: fn())
    m.start()
    return m, library, access


def turn(m, **values):
    m.settings_changed(validate({**DEFAULTS, **values}))


def test_the_switches_start_off_and_are_validated():
    assert switches(DEFAULTS) == (False, False, False)
    assert switches(validate({"mouse_back_forward": True, "mouse_reverse_scrolling": "yes"})) == (False, False, True)


def test_no_tap_while_every_switch_is_off():
    m, library, access = make(allowed=True)
    assert library.tap is None and access.asked == 0
    turn(m, mouse_smooth_scrolling=True)
    assert library.tap == (False, True, False)
    turn(m)
    assert library.tap is None


def test_a_switch_turned_on_asks_for_accessibility_once():
    m, library, access = make()
    turn(m, mouse_reverse_scrolling=True)
    assert access.asked == 1 and library.tap is None
    turn(m, mouse_reverse_scrolling=True, mouse_back_forward=True)
    turn(m)
    turn(m, mouse_smooth_scrolling=True)
    assert access.asked == 1  # after that, the tab's button opens the pane


def test_launch_with_a_switch_on_never_asks():
    m, library, access = make(mouse_reverse_scrolling=True)
    assert access.asked == 0 and library.tap is None


def test_the_tap_starts_once_allowed_and_stops_if_that_is_taken_away():
    m, library, access = make(mouse_reverse_scrolling=True)
    access.grant(True)
    assert library.tap == (True, False, False)
    access.grant(False)
    assert library.tap is None
    access.grant(True)
    assert library.tap == (True, False, False)


def test_a_grant_the_notification_missed_is_seen_by_the_tabs_polls():
    m, library, access = make(mouse_back_forward=True)
    access.is_allowed = True  # no notification
    assert m.status() == {"available": True, "allowed": True, "running": True}
    assert library.tap == (False, False, True)


def test_the_updater_stops_the_tap_and_starts_it_if_the_swap_fails():
    m, library, access = make(allowed=True, mouse_reverse_scrolling=True)
    [(stop, start)] = access.taps
    stop()
    assert library.tap is None
    turn(m, mouse_reverse_scrolling=True, mouse_smooth_scrolling=True)  # nothing starts it before the swap
    access.grant(False)
    access.grant(True)
    assert library.tap is None
    start()
    assert library.tap == (True, True, False)


def test_without_the_dylib_nothing_happens(monkeypatch, caplog):
    monkeypatch.setattr(mouse, "library_path", lambda: None)
    access = FakeAccess(True)
    m = Mouse(FakeSettings(mouse_reverse_scrolling=True), access=access, call_on_main=lambda fn: fn())
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        m.start()
    assert "Mouse isn't available: running from source" in caplog.text
    assert not m.available and access.watchers == [] and access.taps == []
    assert m.status() == {"available": False, "allowed": False, "running": False}
    turn(m, mouse_back_forward=True)
    assert access.asked == 0


def test_app_log_says_each_change_once(caplog):
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        m, library, access = make()
        turn(m, mouse_reverse_scrolling=True)
        access.grant(True)
        access.grant(True)
        turn(m, mouse_reverse_scrolling=True, mouse_back_forward=True)
        turn(m)
    assert [r.getMessage() for r in caplog.records] == [
        "Asked macOS to allow Accessibility for the mouse switches",
        "Mouse is waiting for Accessibility to be allowed (reverse scrolling)",
        "Mouse tap on (reverse scrolling)",
        "Mouse tap on (reverse scrolling, back and forward buttons)",
        "Mouse tap off (every switch is off)",
    ]


def test_describe_and_ask():
    assert describe((True, True, True)) == "reverse scrolling, smooth scrolling, back and forward buttons"
    assert should_ask((False, False, False), (True, False, False), allowed=False, asked=False)
    assert not should_ask((True, False, False), (True, False, False), allowed=False, asked=False)  # nothing turned on
    assert not should_ask((True, False, False), (False, False, False), allowed=False, asked=False)
    assert not should_ask((False, False, False), (True, False, False), allowed=True, asked=False)
    assert not should_ask((False, False, False), (True, False, False), allowed=False, asked=True)


def test_accessibility_stops_and_starts_every_registered_tap(monkeypatch):
    monkeypatch.setattr(accessibility, "_taps", [])
    calls = []
    accessibility.register(lambda: calls.append("stop mouse"), lambda: calls.append("start mouse"))
    accessibility.register(lambda: 1 / 0, lambda: calls.append("start displays"))  # one failing doesn't stop the rest
    accessibility.stop_taps()
    accessibility.start_taps()
    assert calls == ["stop mouse", "start mouse", "start displays"]


def test_accessibility_reads_the_grant():
    assert accessibility.allowed() in (True, False)
