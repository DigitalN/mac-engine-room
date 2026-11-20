"""Menu bar text formatting, and what an update touches. No status item is created:
the update test swaps in a stand-in for it."""

import time
from types import SimpleNamespace

import AppKit

import statusbar
from statusbar import (FIGURE_SPACE, StatusBar, battery_status, fans_text, format_temperature, format_watts,
                       menu_bar_temperature, menu_bar_title, menu_bar_value, slow_menu_note)


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


def test_chip_row_only_shows_on_a_mac_with_a_chip_sensor(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    bar.update({"has_chip_power": False, "has_screen_power": True, "screen_power": 7.2}, None, {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._rows["chip"].isHidden() and not bar._rows["screen"].isHidden()  # each row is its own
    bar.update({"has_chip_power": True, "chip_power": 1.8}, None, {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert not bar._rows["chip"].isHidden()
    assert _row(bar, "chip") == "Chip (CPU + GPU)\t1.8 W"


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


def test_menu_bar_falls_back_to_system_on_a_mac_with_no_chip_sensor():
    s = {"system_load": 27.0, "chip_power": None, "has_chip_power": False, "screen_power": 2.0, "has_screen_power": True}
    settings = {"menu_bar_metric": "chip"}
    assert menu_bar_value(s, settings) == 27.0
    assert menu_bar_title(s, None, settings) == "27.0 W"
    assert settings == {"menu_bar_metric": "chip"}  # the choice itself is kept
    # With a chip sensor it's the chip's, and a missed reading shows as missing, not as another number.
    assert menu_bar_value({**s, "chip_power": 1.8, "has_chip_power": True}, settings) == 1.8
    assert menu_bar_value({**s, "has_chip_power": True}, settings) is None
    # Each falls back on its own sensor: a Mac with no chip sensor still has its screen's.
    assert menu_bar_value(s, {"menu_bar_metric": "screen"}) == 2.0


def test_the_menu_shows_the_three_profiles_chosen_in_settings(monkeypatch):
    import AppKit

    _fake_status_item(monkeypatch)
    status = {"profile": "balanced", "state": "holding", "targets": [1499, 1499]}
    chosen = []
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None,
                    fan_status=lambda: status, on_profile=chosen.append)
    titles = lambda: [b.title() for _, b in bar._profile_tiles]
    # The chosen profile is the tile in the accent colour.
    on = lambda: [b.title() for box, b in bar._profile_tiles if box.fillColor() == AppKit.NSColor.controlAccentColor()]

    bar.update({}, _thermals(), {"fan_profile": "balanced"})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert not bar._profiles_item.isHidden()
    assert titles() == ["macOS default", "Balanced", "Max"] and on() == ["Balanced"]  # the defaults
    assert bar._allow_fans_item.isHidden()

    settings = {"fan_profile": "balanced", "menu_fan_profiles": ["performance", "max", "macos"]}
    bar.update({}, _thermals(), settings)
    bar._target.menuNeedsUpdate_(bar._menu)
    assert titles() == ["Performance", "Max", "macOS default"] and on() == []  # Balanced isn't one of them
    bar._target.chooseProfile_(bar._profile_tiles[1][1])
    assert chosen == ["max"] and on() == ["Max"]
    assert [b.accessibilityValue() for _, b in bar._profile_tiles].count("selected") == 1

    status.update(state="approval")
    bar._target.menuNeedsUpdate_(bar._menu)
    assert not bar._allow_fans_item.isHidden()

    status.update(state="nofans")
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._profiles_item.isHidden() and bar._allow_fans_item.isHidden()


def test_a_parked_gpu_shows_its_last_reading_dimmed_after_a_minute(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    sample = {"system_load": 7.0, "has_battery": True, "battery_level": 80}
    gpu = bar._tiles["gpu"][2]
    bar.update(sample, _thermals(gpu=None, gpu_held={"c": 47.9, "t": time.time() - 10, "sensors": 2}), {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert _tile(bar, "gpu") == "48°" and gpu.textColor() == AppKit.NSColor.labelColor()
    bar.update(sample, _thermals(gpu=None, gpu_held={"c": 47.9, "t": time.time() - 180, "sensors": 2}), {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert _tile(bar, "gpu") == "48°" and gpu.textColor() == AppKit.NSColor.secondaryLabelColor()
    bar.update(sample, _thermals(), {})  # it's back
    bar._target.menuNeedsUpdate_(bar._menu)
    assert _tile(bar, "gpu") == "59°" and gpu.textColor() == AppKit.NSColor.labelColor()


def test_a_desktop_has_no_battery_or_adapter_row(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    bar.update({"system_load": 12.0, "has_battery": False, "external_connected": True}, _thermals(), {})
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._rows["battery"].isHidden() and bar._rows["adapter"].isHidden() and not bar._rows["system"].isHidden()


def test_a_menu_is_only_logged_when_it_was_slow_to_open():
    assert slow_menu_note(0.05) is None
    assert slow_menu_note(0.3) is None
    assert slow_menu_note(0.31) == "Menu took 0.3 s to open"
    assert slow_menu_note(1.84) == "Menu took 1.8 s to open"


def test_the_log_says_how_long_a_click_took_to_open_the_menu(monkeypatch, caplog):
    import logging

    _fake_status_item(monkeypatch)
    now = [500.0]
    monkeypatch.setattr(statusbar.time, "monotonic", lambda: now[0])
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    bar._click_time = lambda: 498.2  # the click was 1.8 s ago
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        bar._target.menuNeedsUpdate_(bar._menu)
        bar._target.menuWillOpen_(bar._menu)
        bar._target.menuDidClose_(bar._menu)
        bar._click_time = lambda: 499.9  # a quick one
        bar._target.menuNeedsUpdate_(bar._menu)
        bar._target.menuWillOpen_(bar._menu)
        bar._click_time = lambda: None  # opened from the keyboard: no click to time
        bar._target.menuNeedsUpdate_(bar._menu)
        bar._target.menuWillOpen_(bar._menu)
    assert [r.getMessage() for r in caplog.records] == ["Menu took 1.8 s to open"]


def test_quitting_takes_the_item_out_of_the_menu_bar(monkeypatch):
    removed = []
    item = SimpleNamespace(button=lambda: _FakeButton(), setMenu_=lambda menu: None)
    monkeypatch.setattr(statusbar, "NSStatusBar", SimpleNamespace(systemStatusBar=lambda: SimpleNamespace(
        statusItemWithLength_=lambda length: item, removeStatusItem_=removed.append)))
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    bar.remove()
    assert removed == [item]


class _TitleButton(_FakeButton):
    def __init__(self):
        super().__init__()
        self.attributed = []

    def setAttributedTitle_(self, text):
        self.attributed.append(text.string())


def test_menu_order_open_dashboard_thermals_keep_awake_power(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    titles = [i.title() if i.view() is None else "[view]" for i in bar._menu.itemArray() if not i.isSeparatorItem()]
    assert titles[0] == "Open Dashboard"
    assert titles.index("Thermals") < titles.index("Keep Awake") < titles.index("Power")
    assert titles[-2:] == ["Check for Updates…", "Quit Mac Engine Room"]
    assert bar._menu.itemArray()[0].keyEquivalent() == "d"


def test_keep_awake_section_lights_the_choice_and_shows_the_line(monkeypatch):
    _fake_status_item(monkeypatch)
    status = {"choice": "off", "holding": False, "line": None}
    chosen = []

    def choose(choice):
        chosen.append(choice)
        status.update(choice=choice, holding=True, line="Keep Awake ends when the Mac sleeps")
        return dict(status)

    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None,
                    keep_awake_status=lambda: dict(status), on_keep_awake=choose)
    bar.update({}, None, {})
    bar._target.menuNeedsUpdate_(bar._menu)
    lit = lambda: [c for c, (box, _) in bar._keep_awake_tiles.items() if box.fillColor() == AppKit.NSColor.controlAccentColor()]
    assert lit() == ["off"] and bar._keep_awake_line_item.isHidden()
    button = bar._keep_awake_tiles["sleep"][1]
    bar._target.chooseKeepAwake_(button)
    assert chosen == ["sleep"] and lit() == ["sleep"]
    assert not bar._keep_awake_line_item.isHidden()
    assert bar._keep_awake_line.stringValue() == "Keep Awake ends when the Mac sleeps"
    status.update(choice=None, line="Keep Awake ends at 5 PM (4 hours 38 minutes)")
    bar._target.menuNeedsUpdate_(bar._menu)
    assert lit() == [] and bar._keep_awake_line.stringValue() == "Keep Awake ends at 5 PM (4 hours 38 minutes)"


def test_cup_follows_the_reading_only_while_keep_awake_holds(monkeypatch):
    button = _TitleButton()
    item = SimpleNamespace(button=lambda: button, setMenu_=lambda menu: None)
    monkeypatch.setattr(statusbar, "NSStatusBar", SimpleNamespace(
        systemStatusBar=lambda: SimpleNamespace(statusItemWithLength_=lambda length: item)))
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    sample = {"system_load": 12.3}
    bar.update(sample, None, {}, keep_awake=True)
    bar.update(sample, None, {}, keep_awake=True)  # nothing new for AppKit
    assert button.attributed == [f"12.3 W ￼"]  # the reading, a space, the cup's attachment
    bar.set_keep_awake(False)
    assert button.titles[-1] == "12.3 W"
    bar.set_keep_awake(False)
    assert button.titles.count("12.3 W") == 1


def test_the_cup_has_steam_and_falls_back_to_a_plain_one(monkeypatch):
    assert statusbar.KEEP_AWAKE_SYMBOLS == ("cup.and.heat.waves.fill", "cup.and.saucer.fill")
    asked, real = [], StatusBar._symbol

    def symbol(name, point_size, description=None):
        asked.append(name)
        return None if name == "cup.and.heat.waves.fill" else real(name, point_size, description)  # macOS 12 or 13

    _fake_status_item(monkeypatch)
    monkeypatch.setattr(StatusBar, "_symbol", staticmethod(symbol))
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None)
    assert bar._cup is not None
    assert asked.index("cup.and.heat.waves.fill") < asked.index("cup.and.saucer.fill")


_BUILTIN = {"id": 1, "name": "Built-in Display", "builtin": True, "adjustable": True, "percent": 100.0, "top": 160.0}
_TV = {"id": 5, "name": "LG TV SSCR2", "builtin": False, "adjustable": True, "percent": 60.0, "top": 100.0}


def test_displays_get_a_slider_each_and_the_built_in_one_runs_to_160(monkeypatch):
    _fake_status_item(monkeypatch)
    shown = [_BUILTIN, {**_TV, "adjustable": False, "percent": None}]
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None, displays=lambda: shown)
    bar._target.menuNeedsUpdate_(bar._menu)
    assert not bar._displays_header.isHidden()
    [(item, display, (name, percent, slider))] = bar._display_items  # a display that doesn't answer DDC gets none
    assert (display, name.stringValue(), percent.stringValue()) == (1, "Built-in Display", "100%")
    assert (slider.minValue(), slider.maxValue(), slider.doubleValue()) == (0, 160, 100)
    # The TV answers: the items are made again, in macOS's order.
    shown[1] = _TV
    bar._target.menuNeedsUpdate_(bar._menu)
    assert [d for _, d, _ in bar._display_items] == [1, 5]
    assert bar._display_items[1][2][2].maxValue() == 100
    order = list(bar._menu.itemArray())
    assert order.index(bar._displays_header) + 1 == order.index(bar._display_items[0][0])


def test_no_display_to_adjust_hides_the_section(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None, displays=lambda: [])
    bar._target.menuNeedsUpdate_(bar._menu)
    assert bar._displays_header.isHidden() and bar._display_items == []


def test_moving_a_slider_sets_whole_percents_and_shows_them_at_once(monkeypatch):
    _fake_status_item(monkeypatch)
    set_to = []
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None,
                    displays=lambda: [_BUILTIN, _TV], on_display_brightness=lambda d, p: set_to.append((d, p)))
    bar._target.menuNeedsUpdate_(bar._menu)
    slider = bar._display_items[0][2][2]
    slider.setDoubleValue_(131.6)
    bar._target.slideDisplay_(slider)
    assert set_to == [(1, 132.0)]
    assert bar._display_items[0][2][1].stringValue() == "132%"


def test_a_dragged_slider_sits_on_100_near_it_with_the_trackpads_tap_as_it_lands(monkeypatch):
    _fake_status_item(monkeypatch)
    set_to, taps = [], []
    monkeypatch.setattr(StatusBar, "_pointer_holds", staticmethod(lambda slider: True))
    monkeypatch.setattr(statusbar.display_tool, "haptic", lambda: taps.append(1))
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None,
                    displays=lambda: [{**_BUILTIN, "percent": 90.0}, _TV], on_display_brightness=lambda d, p: set_to.append((d, p)))
    bar._target.menuNeedsUpdate_(bar._menu)
    builtin, tv = bar._display_items[0][2][2], bar._display_items[1][2][2]
    for raw in (97.4, 101.6, 104.2, 99.3):
        builtin.setDoubleValue_(raw)
        bar._target.slideDisplay_(builtin)
        if raw < 104:
            assert builtin.doubleValue() == 100  # back on the mark while the pointer is in the zone
    assert set_to == [(1, 100.0), (1, 104.0), (1, 100.0)]  # staying on the mark sends nothing new
    assert len(taps) == 2  # landing from 90, then again from 104; not while it stays on 100
    assert bar._display_items[0][2][1].stringValue() == "100%"
    tv.setDoubleValue_(98.0)  # a TV ends at 100: nothing to snap to
    bar._target.slideDisplay_(tv)
    assert set_to[-1] == (5, 98.0) and len(taps) == 2


def test_a_slider_moved_without_the_pointer_never_snaps(monkeypatch):
    _fake_status_item(monkeypatch)
    set_to = []
    monkeypatch.setattr(statusbar.display_tool, "haptic", lambda: set_to.append("tap"))
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None,
                    displays=lambda: [_BUILTIN], on_display_brightness=lambda d, p: set_to.append((d, p)))
    bar._target.menuNeedsUpdate_(bar._menu)
    slider = bar._display_items[0][2][2]
    slider.setDoubleValue_(101.0)  # VoiceOver's step up from 100: no pointer event comes with it
    bar._target.slideDisplay_(slider)
    assert set_to == [(1, 101.0)] and slider.doubleValue() == 101


def test_a_key_press_shows_in_the_open_menu(monkeypatch):
    _fake_status_item(monkeypatch)
    bar = StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None, displays=lambda: [_BUILTIN])
    bar._target.menuNeedsUpdate_(bar._menu)
    bar.set_displays([{**_BUILTIN, "percent": 110.0}])  # closed: nothing changes
    assert bar._display_items[0][2][1].stringValue() == "100%"
    bar.menu_open = True
    bar.set_displays([{**_BUILTIN, "percent": 110.0}])
    assert bar._display_items[0][2][1].stringValue() == "110%"
    assert bar._display_items[0][2][2].doubleValue() == 110
