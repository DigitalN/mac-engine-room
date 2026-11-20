"""displays.py: which Macs boost, what's saved as the boost and when it comes back, asking for
Accessibility, and the keys tap following the switch, the grant and the updater, against a stand-in
for libdisplays.dylib. Native/Displays' own pure parts are in tests/displays/main.swift."""

import logging

import displays
from displays import Displays

BUILTIN = {"id": 1, "name": "Built-in Display", "builtin": True, "adjustable": True, "answered": True,
           "percent": 100.0, "top": 160.0, "nits": 500, "volume": None, "muted": None, "boosting": False}
TV = {"id": 5, "name": "LG TV SSCR2", "builtin": False, "adjustable": True, "answered": True, "percent": 60.0,
      "top": 100.0, "nits": None, "volume": 25, "muted": False, "boosting": False}


class FakeSettings:
    def __init__(self, **values):
        self.values = {"display_keys": False, "display_boost": 1.0, **values}
        self.updates = []

    def get(self):
        return dict(self.values)

    def update(self, changes):
        self.updates.append(changes)
        self.values.update(changes)
        return self.get()


class FakeLibrary:
    """libdisplays.dylib as displays.Library drives it."""

    def __init__(self, shown=None, allowed=True):
        self.shown = [dict(d) for d in (shown or [BUILTIN])]
        self.allowed = allowed
        self.keys = False
        self.calls = []
        self.changed = None

    def start(self, chip_ignores_gamma, changed, log_line):
        self.calls.append(("start", chip_ignores_gamma))
        self.changed = changed
        changed()  # the dylib finds the displays at once

    def list(self):
        return [dict(d) for d in self.shown]

    def refresh(self):
        self.calls.append(("refresh",))

    def set_brightness(self, display, percent):
        self.calls.append(("brightness", display, percent))
        for d in self.shown:
            if d["id"] == display:
                d["percent"] = percent
                d["boosting"] = d["builtin"] and percent > 100

    def set_volume(self, display, percent):
        self.calls.append(("volume", display, percent))

    def set_keys(self, on):
        if not on:
            self.keys = False
            return displays.OFF
        if not self.allowed:
            return displays.NOT_ALLOWED
        self.keys = True
        return displays.ON

    def stop_keys(self):
        self.keys = False

    def keys_running(self):
        return self.keys


class FakeAccess:
    def __init__(self, allowed=True):
        self.granted = allowed
        self.asked = 0
        self.watchers = []
        self.taps = []

    def allowed(self):
        return self.granted

    def ask(self):
        self.asked += 1
        return self.granted

    def watch(self, callback):
        self.watchers.append(callback)

    def register(self, stop, start):
        self.taps.append((stop, start))

    def setting_name(self):
        return "Device Control and Data Access"


def make(settings=None, library=None, access=None, chip="Apple M1 Pro"):
    later = []
    tool = Displays(settings or FakeSettings(), library=library or FakeLibrary(), access=access or FakeAccess(),
                    call_on_main=lambda fn: fn(), call_later=lambda delay, fn: later.append(fn), chip=chip)
    return tool, later


# ── pure parts ──

def test_m5_pro_m5_max_and_the_neo_ignore_gamma_tables():
    assert displays.chip_ignores_gamma("Apple M5 Pro")
    assert displays.chip_ignores_gamma("Apple M5 Max")
    assert displays.chip_ignores_gamma("Apple A18 Pro")
    assert not displays.chip_ignores_gamma("Apple M5")  # the base M5 works
    assert not displays.chip_ignores_gamma("Apple M1 Pro")
    assert not displays.chip_ignores_gamma(None)


def test_the_boost_is_the_built_in_displays_percent_over_100():
    assert displays.boost_of([{**BUILTIN, "percent": 130.0}]) == 1.3
    assert displays.boost_of([{**BUILTIN, "percent": 80.0}]) == 1.0
    assert displays.boost_of([{**BUILTIN, "percent": 100.0, "top": 100.0}]) == 1.0  # a display that can't boost
    assert displays.boost_of([TV]) == 1.0
    assert displays.boost_of([]) == 1.0


def test_a_dragged_slider_sits_on_100_within_3_points_of_it_on_a_display_that_boosts():
    assert displays.SNAP_WITHIN == 3
    for value in (97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0):  # inside the zone, edges included
        assert displays.snap(value, 160, held=True, before=90.0)[0] == 100.0
    for value in (96.0, 96.9, 103.1, 104.0, 0.0, 160.0):  # outside it
        assert displays.snap(value, 160, held=True, before=90.0) == (value, False)


def test_the_tap_comes_only_as_the_slider_lands_on_100():
    assert displays.snap(98.0, 160, held=True, before=95.0) == (100.0, True)  # entering from below
    assert displays.snap(102.0, 160, held=True, before=110.0) == (100.0, True)  # and from above
    assert displays.snap(101.0, 160, held=True, before=100.0) == (100.0, False)  # already on it
    assert displays.snap(99.0, 160, held=True) == (100.0, True)  # nothing known before


def test_a_display_that_ends_at_100_never_snaps():
    for value in (97.0, 98.0, 99.0):
        assert displays.snap(value, 100, held=True, before=90.0) == (value, False)


def test_the_arrow_keys_and_voiceover_never_snap():
    # They step one point at a time: from 100, a step to 101 would be put back on 100 and never get past it.
    for value in (97.0, 99.0, 101.0, 103.0):
        assert displays.snap(value, 160, held=False, before=100.0) == (value, False)


def test_the_boost_comes_back_only_with_macoss_brightness_at_its_maximum():
    assert displays.can_bring_back([BUILTIN], 1.3)
    assert not displays.can_bring_back([{**BUILTIN, "percent": 90.0}], 1.3)
    assert not displays.can_bring_back([{**BUILTIN, "top": 100.0}], 1.3)
    assert not displays.can_bring_back([TV], 1.3)
    assert not displays.can_bring_back([BUILTIN], 1.0)


def test_percents_are_whole_unless_a_quarter_step_left_a_half():
    assert displays.menu_label({"percent": 130.0}) == "130%"
    assert displays.menu_label({"percent": 42.5}) == "42.5%"
    assert displays.menu_label({"percent": None}) == "—"


def test_accessibility_is_asked_for_once_when_the_switch_is_turned_on():
    assert displays.should_ask(False, True, allowed=False, asked=False)
    assert not displays.should_ask(False, True, allowed=True, asked=False)
    assert not displays.should_ask(False, True, allowed=False, asked=True)
    assert not displays.should_ask(True, True, allowed=False, asked=False)
    assert not displays.should_ask(True, False, allowed=False, asked=False)


# ── the controller ──

def test_the_chip_list_reaches_the_dylib():
    library = FakeLibrary()
    make(library=library, chip="Apple M5 Max")[0].start()
    assert library.calls[0] == ("start", True)


def test_a_saved_boost_comes_back_at_launch_and_isnt_saved_over_first(caplog):
    settings, library = FakeSettings(display_boost=1.3), FakeLibrary()
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        make(settings, library)[0].start()
    assert ("brightness", 1, 130.0) in library.calls
    assert settings.updates == []  # the dylib's first word (at 100%) didn't save 1.0 over it
    assert "Display boost brought back at 130%" in caplog.text


def test_a_boost_that_cant_come_back_is_let_go(caplog):
    settings = FakeSettings(display_boost=1.3)
    library = FakeLibrary([{**BUILTIN, "percent": 70.0}])
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        make(settings, library)[0].start()
    assert not any(c[0] == "brightness" for c in library.calls)
    assert settings.values["display_boost"] == 1.0
    assert "Display boost not brought back: macOS's brightness is below its maximum" in caplog.text


def test_moving_into_the_boost_is_saved_once_a_second_later():
    settings, library = FakeSettings(), FakeLibrary()
    tool, later = make(settings, library)
    tool.start()
    for percent in (110, 120, 135):
        tool.set_brightness(1, percent)
    assert settings.updates == [] and len(later) == 1
    later.pop()()
    assert settings.updates == [{"display_boost": 1.35}]


def test_the_boost_ending_is_saved_too():
    settings = FakeSettings(display_boost=1.3)
    library = FakeLibrary()
    tool, later = make(settings, library)
    tool.start()
    library.shown[0]["percent"] = 100.0  # macOS's brightness went down: the dylib ended the boost
    library.changed()
    later.pop()()
    assert settings.values["display_boost"] == 1.0


def test_listeners_hear_each_change():
    tool, _ = make(library=FakeLibrary([BUILTIN, TV]))
    heard = []
    tool.on_change(heard.append)
    tool.start()
    tool.set_volume(5, 40)
    assert heard and heard[-1][1]["name"] == "LG TV SSCR2"


def test_no_tap_while_the_switch_is_off():
    library = FakeLibrary()
    make(library=library)[0].start()
    assert not library.keys_running()


def test_turning_the_switch_on_asks_once_then_the_tap_starts_when_allowed(caplog):
    settings, library, access = FakeSettings(), FakeLibrary(allowed=False), FakeAccess(allowed=False)
    tool, _ = make(settings, library, access)
    tool.start()
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        tool.settings_changed({"display_keys": True})
        assert access.asked == 1 and not library.keys_running()
        assert "Display keys are waiting for Accessibility to be allowed" in caplog.text
        tool.settings_changed({"display_keys": False})
        tool.settings_changed({"display_keys": True})
        assert access.asked == 1  # once a launch
        access.granted = library.allowed = True
        access.watchers[0](True)
        assert library.keys_running()
        # What the tap takes is the dylib's line (it knows where the sound goes), not a fixed one here.
        assert "Display keys on" not in caplog.text
        tool.settings_changed({"display_keys": False})
    assert "Display keys off" in caplog.text


def test_the_switch_on_at_launch_never_asks():
    access = FakeAccess(allowed=False)
    make(FakeSettings(display_keys=True), FakeLibrary(allowed=False), access)[0].start()
    assert access.asked == 0


def test_the_tap_goes_when_accessibility_is_taken_away():
    library, access = FakeLibrary(), FakeAccess()
    make(FakeSettings(display_keys=True), library, access)[0].start()
    assert library.keys_running()
    access.granted = library.allowed = False
    access.watchers[0](False)
    assert not library.keys_running()


def test_the_updater_stops_the_tap_and_a_failed_swap_brings_it_back():
    library, access = FakeLibrary(), FakeAccess()
    make(FakeSettings(display_keys=True), library, access)[0].start()
    [(stop, start)] = access.taps
    stop()
    assert not library.keys_running()
    start()
    assert library.keys_running()


def test_the_tab_gets_the_displays_and_the_grant():
    tool, _ = make(library=FakeLibrary([BUILTIN, TV]))
    tool.start()
    status = tool.status()
    assert status["available"] and status["allowed"] and not status["running"]
    assert status["setting"] == "Device Control and Data Access"  # what the tab calls the list
    assert [d["name"] for d in status["displays"]] == ["Built-in Display", "LG TV SSCR2"]


def test_without_the_dylib_there_is_nothing(monkeypatch):
    monkeypatch.setattr(displays, "library_path", lambda: None)
    tool = Displays(FakeSettings(), access=FakeAccess(), call_on_main=lambda fn: fn())
    tool.start()
    assert not tool.available
    assert tool.status() == {"available": False, "displays": [], "allowed": False, "running": False,
                             "setting": "Device Control and Data Access"}
    tool.set_brightness(1, 50)  # nothing to do, and no error
    assert tool.displays() == []


def test_macos_27_calls_the_accessibility_list_device_control_and_data_access(monkeypatch):
    import accessibility

    for version, name in [("27.2.1", "Device Control and Data Access"), ("28.0", "Device Control and Data Access"),
                          ("26.4", "Accessibility"), ("15.7.2", "Accessibility"), ("", "Accessibility")]:
        monkeypatch.setattr(accessibility.platform, "mac_ver", lambda v=version: (v, ("", "", ""), ""))
        assert accessibility.setting_name() == name, version
