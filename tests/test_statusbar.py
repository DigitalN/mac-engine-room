"""Menu bar text formatting, and what an update touches. No status item is created:
the update test swaps in a stand-in for it."""

import time
from types import SimpleNamespace

import statusbar
from statusbar import (FIGURE_SPACE, StatusBar, battery_status, fans_text, format_temperature, format_watts,
                       menu_bar_temperature, menu_bar_title, menu_bar_value)


class _FakeButton:
    def __init__(self):
        self.titles = []

    def setTitle_(self, title):
        self.titles.append(title)

    def __getattr__(self, name):  # setFont_, setImage_ and the rest: ignored
        return lambda *args: None


def _fake_status_item(monkeypatch):
    button = _FakeButton()
    item = SimpleNamespace(button=lambda: button, setMenu_=lambda menu: None)
    monkeypatch.setattr(statusbar, "NSStatusBar", SimpleNamespace(
        systemStatusBar=lambda: SimpleNamespace(statusItemWithLength_=lambda length: item)))
    return button


def _row(bar, key):
    _, _, label, value = bar._row_views[key]
    return f"{label.stringValue()}\t{value.stringValue()}"


def _tile(bar, key):
    return bar._tiles[key][2].stringValue()


def test_screen_row_only_shows_on_a_mac_with_a_screen_sensor(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    bar.update({"has_screen_power": False}, None, {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._rows["screen"].isHidden()
    bar.update({"has_screen_power": True, "screen_power": 7.2}, None, {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert not bar._rows["screen"].isHidden()
    assert _row(bar, "screen") == "Screen\t7.2 W"


def test_update_only_redraws_what_changed(monkeypatch):
    button = _fake_status_item(monkeypatch)
    menu_events = []
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None,
                    on_menu_open=lambda: menu_events.append(bar.menu_open))
    settings = {"menu_bar_metric": "system"}
    sample = {"system_load": 7.04, "screen_power": 2.0, "chip_power": 3.0, "has_battery": True,
              "battery_level": 80}

    bar.update(sample, None, settings)
    bar.update({**sample, "system_load": 6.98}, None, settings)  # still "7.0 W"
    assert button.titles == ["— W", f"{FIGURE_SPACE}7.0 W"]
    assert _row(bar, "system") == "System\t—"  # menu closed: rows untouched

    bar._target.menuNeedsUpdate_(bar._menu)  # what AppKit calls as the menu opens
    bar._target.menuWillOpen_(bar._menu)
    assert _row(bar, "system") == "System\t7.0 W"
    bar.update({**sample, "system_load": 9.5}, None, settings)
    assert _row(bar, "system") == "System\t9.5 W"  # open: rows follow along

    bar._target.menuDidClose_(bar._menu)
    bar.update({**sample, "system_load": 12.0}, None, settings)
    assert _row(bar, "system") == "System\t9.5 W"
    assert menu_events == [True, False]


def _thermals(**values):
    return {"t": time.time(), "cpu": 61.4, "gpu": 58.5, "memory": 50.0, "ssd": 34.2, "ambient": 27.0,
            "hottest": 61.4, "available": list(statusbar.TILES),
            "fans": [{"rpm": 1517, "min": 1499, "max": 4296, "manual": False},
                     {"rpm": 1649, "min": 1499, "max": 4744, "manual": False}], **values}


def test_tiles_show_temperatures_and_fans_and_only_pass_changes_to_appkit(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    sample = {"system_load": 7.0, "has_battery": True, "battery_level": 80}
    bar.update(sample, _thermals(), {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert [_tile(bar, k) for k in statusbar.TILES] == ["61°", "59°", "50°", "34°", "27°", "1649"]
    bar.update(sample, _thermals(), {"temperature_unit": "fahrenheit"})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert _tile(bar, "cpu") == "143°"
    bar.update(sample, _thermals(), {})
    bar._target.menuNeedsUpdate_(bar._menu)

    writes = []
    cpu = bar._tiles["cpu"][2]
    bar._put = lambda view, setter, value, put=bar._put: (writes.append(value) if view is cpu else None, put(view, setter, value))[1]
    bar._target.menuWillOpen_(bar._menu)
    bar.update(sample, _thermals(cpu=61.3), {})  # still 61°
    bar.update(sample, _thermals(cpu=62.0), {})
    assert writes == ["61°", "62°"] and cpu.stringValue() == "62°"


def test_a_mac_without_fans_has_no_fans_tile_and_stale_readings_show_dashes(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    fanless = _thermals(available=["cpu", "gpu", "memory", "ssd"], fans=[], t=time.time() - 60)
    bar.update({}, fanless, {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._tiles["fans"][0].isHidden() and bar._tiles["ambient"][0].isHidden()
    assert not bar._tiles["ssd"][0].isHidden()
    assert _tile(bar, "cpu") == "—°"  # read a minute ago, before the menu opened
    bar.update({}, {**fanless, "t": time.time()}, {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert _tile(bar, "cpu") == "61°"


def test_a_mac_without_temperatures_hides_the_thermals_section(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    bar.update({}, _thermals(available=[], fans=[]), {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._thermals_header.isHidden() and bar._tiles_item.isHidden()


def test_temperatures_in_the_menu_bar_keep_their_width():
    assert format_temperature(61.4, pad_to=3) == f"{FIGURE_SPACE}61°"
    assert format_temperature(100.2, pad_to=3) == "100°"
    assert format_temperature(61.4, fahrenheit=True, pad_to=3) == "143°"
    assert menu_bar_title({}, _thermals(), {"menu_bar_metric": "cpu_temp", "temperature_unit": "fahrenheit"}) == "143°"
    assert format_temperature(60.5) == "61°" and format_temperature(None, pad_to=3) == "—°"
    assert len(format_temperature(9.0, pad_to=3)) == len(format_temperature(130.0, fahrenheit=True, pad_to=3))


def test_menu_bar_temperature_choices():
    sample = {"is_charging": False, "system_load": 7.0}
    thermals = _thermals(cpu=61.4, hottest=70.2)
    assert menu_bar_title(sample, thermals, {"menu_bar_metric": "cpu_temp"}) == f"{FIGURE_SPACE}61°"
    assert menu_bar_title(sample, thermals, {"menu_bar_metric": "hottest_temp"}) == f"{FIGURE_SPACE}70°"
    assert menu_bar_title(sample, None, {"menu_bar_metric": "cpu_temp"}) == "—°"
    assert menu_bar_title(sample, thermals, {"menu_bar_metric": "system"}) == f"{FIGURE_SPACE}7.0 W"
    # Adapter input while charging wins over a temperature too, and then none is needed.
    charging = {"is_charging": True, "system_in": 95.0}
    settings = {"menu_bar_metric": "cpu_temp", "menu_bar_show_charging": True}
    assert menu_bar_temperature(charging, settings) is None
    assert menu_bar_title(charging, thermals, settings) == "95.0 W"
    assert menu_bar_temperature(charging, {**settings, "menu_bar_show_charging": False}) == "cpu"


def test_fans_text():
    assert fans_text([{"rpm": 0}, {"rpm": 0}]) == "Off"
    assert fans_text([{"rpm": 1517}, {"rpm": 1649}]) == "1649"  # the fastest: two don't fit in a tile
    assert fans_text([{"rpm": 1517}, {"rpm": 0}]) == "1517"
    assert fans_text([{"rpm": None}]) == "—"


def test_title_width_is_stable():
    assert FIGURE_SPACE == "\u2007"  # a plain space is narrower than a digit
    assert format_watts(9.84) == f"{FIGURE_SPACE}9.8 W"
    assert len(format_watts(9.8)) == len(format_watts(27.1))
    assert format_watts(None) == "— W"


def test_battery_status_states():
    base = {"battery_level": 63, "time_remaining_min": 64, "fully_charged": False,
            "is_charging": True, "external_connected": True}
    assert battery_status(base) == "63% · 1:04 to full"
    assert battery_status({**base, "time_remaining_min": None}) == "63% · Charging"
    assert battery_status({**base, "is_charging": False}) == "63% · Not charging"
    assert battery_status({**base, "is_charging": False, "external_connected": False,
                           "time_remaining_min": 185}) == "63% · 3:05 left"
    assert battery_status({**base, "battery_level": 100, "fully_charged": True}) == "100% · Fully charged"
    assert battery_status({"battery_level": None}) == "No battery"


def test_battery_status_with_a_charge_limit():
    base = {"battery_level": 63, "time_remaining_min": 12, "fully_charged": False,
            "is_charging": True, "external_connected": True, "charge_limit": 80}
    assert battery_status(base) == "63% · 0:12 to 80%"
    assert battery_status({**base, "battery_level": 78, "is_charging": False,
                           "at_charge_limit": True}) == "78% · Held at 80% limit"
    assert battery_status({**base, "is_charging": False}) == "63% · Not charging"
    # Whatever limit is set in System Settings is the one named.
    assert battery_status({**base, "charge_limit": 85}) == "63% · 0:12 to 85%"
    assert battery_status({**base, "battery_level": 85, "is_charging": False, "charge_limit": 85,
                           "at_charge_limit": True}) == "85% · Held at 85% limit"


def test_menu_bar_value_prefers_adapter_input_while_charging():
    s = {"is_charging": True, "system_in": 105.0, "system_load": 27.0, "screen_power": 11.0,
         "has_screen_power": True}
    assert menu_bar_value(s, {"menu_bar_show_charging": True, "menu_bar_metric": "system"}) == 105.0
    assert menu_bar_value(s, {"menu_bar_show_charging": False, "menu_bar_metric": "screen"}) == 11.0
    assert menu_bar_value({**s, "is_charging": False}, {"menu_bar_show_charging": True,
                                                        "menu_bar_metric": "system"}) == 27.0


def test_menu_bar_falls_back_to_system_on_a_mac_with_no_screen_sensor():
    s = {"system_load": 27.0, "screen_power": None, "has_screen_power": False}
    assert menu_bar_value(s, {"menu_bar_metric": "screen"}) == 27.0
    # A missed reading on a Mac that has the sensor shows as missing, not as another number.
    assert menu_bar_value({**s, "has_screen_power": True}, {"menu_bar_metric": "screen"}) is None


def test_fan_profiles_show_with_fans_and_follow_the_setting(monkeypatch):
    import AppKit

    _fake_status_item(monkeypatch)
    status = {"profile": "balanced", "state": "watching", "start": 55}
    chosen = []
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None,
                    fan_status=lambda: status, on_profile=chosen.append)
    on = lambda: [b.title() for b in bar._profile_buttons if b.state() == AppKit.NSControlStateValueOn]

    bar.update({}, _thermals(), {"fan_profile": "balanced"})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert not bar._profiles_item.isHidden() and on() == ["Balanced"]
    assert bar._manual_item.isHidden() and bar._allow_fans_item.isHidden()

    bar._target.chooseProfile_(bar._profile_buttons[5])  # Manual
    assert chosen == ["manual"] and on() == ["Manual"] and not bar._manual_item.isHidden()
    assert bar._manual_label.stringValue() == "1,499 rpm"  # the slider at its left: each fan's minimum

    status.update(state="approval")
    bar.update({}, _thermals(), {"fan_profile": "manual", "fan_manual_level": 1.0})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert not bar._allow_fans_item.isHidden()
    assert bar._manual_label.stringValue() == "4,296 and 4,744 rpm"

    status.update(state="nofans")
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._profiles_item.isHidden() and bar._manual_item.isHidden() and bar._allow_fans_item.isHidden()
