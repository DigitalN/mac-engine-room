"""Menu bar item: one reading in the title (watts, or a temperature), and a menu.

A plain NSStatusItem with a native NSMenu. The Rust app used a Tauri tray icon
plus an NSPopover, and on macOS 27 AppKit swallowed the clicks so the panel
never opened; a standard status-item menu has no such dependency.

The menu's top rows are custom views: Thermals' fan profile buttons (the three chosen in
Settings), then the temperatures as tiles with a small grey label above each value (fans in
the last tile); then Power rows with an SF Symbol, a grey label and the
value on the right, then the actions. They're only filled in while the menu is open, and
only values that changed are passed to AppKit.

Every method here must run on the main thread (main.py dispatches with
AppHelper.callAfter).
"""

import logging
import math
import time

import AppKit
from AppKit import (
    NSBox, NSButton, NSColor, NSFont, NSImage, NSImageSymbolConfiguration, NSImageView, NSMenu,
    NSMenuItem, NSStatusBar, NSTextField, NSVariableStatusItemLength, NSView,
)
from Foundation import NSObject

import fans as fan_control

log = logging.getLogger("pythonflow")

FIGURE_SPACE = "\u2007"  # same width as a digit in a monospaced-digit font
TEMPERATURE_METRICS = {"cpu_temp": "cpu", "hottest_temp": "hottest"}
TILES = ("cpu", "gpu", "memory", "ssd", "ambient", "fans")
TILE_LABELS = {"cpu": "CPU", "gpu": "GPU", "memory": "Memory", "ssd": "SSD", "ambient": "Ambient",
               "fans": "Fans · rpm"}
ROWS = ("battery", "system", "screen", "chip", "adapter")
ROW_LABELS = {"battery": "Battery", "system": "System", "screen": "Screen",
              "chip": "Chip (CPU + GPU)", "adapter": "Adapter"}
ROW_SYMBOLS = {"system": "laptopcomputer", "screen": "sun.max", "chip": "cpu", "adapter": "powerplug"}
# A reading older than this isn't shown: it was read before the menu last opened.
THERMALS_FRESH_FOR = 6.0
HELD_NOTE_AFTER = 60.0  # seconds: a parked GPU's last reading, shown dimmed after this (as the page says its age)
SLOW_MENU = 0.3  # seconds from the click to an open, filled menu above which app.log says so

MENU_WIDTH = 300
INSET = 14
TILE_COLUMNS, TILE_GAP, TILE_HEIGHT = 3, 6, 42
ROW_HEIGHT = 22
PROFILE_HEIGHT = 24
# Fan control states with the profile buttons available to click.
PROFILES_USABLE = ("off", "holding", "override", "other", "approval", "error")


def slow_menu_note(took):
    """The line for app.log when the menu took `took` seconds to open, or None if it was quick."""
    return f"Menu took {took:.1f} s to open" if took > SLOW_MENU else None


def format_watts(value, pad_to=4):
    """'27.1 W', padded with figure spaces so the menu bar width stays put."""
    if not isinstance(value, (int, float)):
        return "— W"
    return f"{value:.1f}".rjust(pad_to, FIGURE_SPACE) + " W"


def uses_fahrenheit(settings):
    return settings.get("temperature_unit") == "fahrenheit"


def format_temperature(celsius, fahrenheit=False, pad_to=0):
    """'58°', in Fahrenheit if asked, padded to `pad_to` digits with figure spaces. Halves
    round up, as on the dashboard."""
    if not isinstance(celsius, (int, float)):
        return "—°"
    value = celsius * 9 / 5 + 32 if fahrenheit else celsius
    return str(math.floor(value + 0.5)).rjust(pad_to, FIGURE_SPACE) + "°"


def format_minutes(minutes):
    if not minutes:
        return None
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}:{mins:02d}"


def battery_status(sample):
    """Short human status, e.g. '63% · 1:04 to full', or '63% · 0:12 to 80%' with a charge limit."""
    level = sample.get("battery_level")
    if level is None:
        return "No battery"
    remaining = format_minutes(sample.get("time_remaining_min"))
    limit = sample.get("charge_limit")
    if sample.get("fully_charged"):
        state = "Fully charged"
    elif sample.get("is_charging"):
        state = f"{remaining} to {f'{limit}%' if limit else 'full'}" if remaining else "Charging"
    elif sample.get("at_charge_limit"):
        state = f"Held at {limit}% limit"
    elif sample.get("external_connected"):
        state = "Not charging"
    else:
        state = f"{remaining} left" if remaining else "On battery"
    return f"{level}% · {state}"


def battery_symbol(sample):
    if sample.get("is_charging"):
        return "battery.100percent.bolt"
    level = sample.get("battery_level") or 0
    return f"battery.{min(100, (level + 12) // 25 * 25)}percent"


def fans_text(fans):
    """The fastest fan's speed, or 'Off' when they've all stopped (Apple silicon stops them
    when cool). Two speeds don't fit in a tile; the dashboard shows each fan."""
    speeds = [f["rpm"] for f in fans if f.get("rpm") is not None]
    if not speeds:
        return "—"
    return str(max(speeds)) if any(speeds) else "Off"


def menu_bar_temperature(sample, settings):
    """Which temperature the menu bar shows ("cpu" or "hottest"), or None while it shows watts."""
    if settings.get("menu_bar_show_charging") and sample.get("is_charging"):
        return None
    return TEMPERATURE_METRICS.get(settings.get("menu_bar_metric"))


def menu_bar_value(sample, settings):
    """The watts shown in the menu bar for the user's chosen metric."""
    if settings.get("menu_bar_show_charging") and sample.get("is_charging"):
        return sample.get("system_in")
    metric = settings.get("menu_bar_metric")
    if metric == "screen" and not sample.get("has_screen_power"):
        metric = "system"  # a Mac with no screen sensor doesn't offer it; the setting stays as it was
    if metric == "chip" and not sample.get("has_chip_power"):
        metric = "system"  # nor does one with no chip sensor
    return {
        "system": sample.get("system_load"),
        "screen": sample.get("screen_power"),
        "chip": sample.get("chip_power"),
    }.get(metric, sample.get("system_load"))


def menu_bar_title(sample, thermals, settings):
    """A temperature keeps three digits' width (130 °C is 266 °F), watts four."""
    reading = menu_bar_temperature(sample, settings)
    if reading:
        return format_temperature((thermals or {}).get(reading), uses_fahrenheit(settings), pad_to=3)
    return format_watts(menu_bar_value(sample, settings))


class _MenuTarget(NSObject):
    """Receives menu item actions and the menu's delegate calls, and forwards them to
    Python callbacks."""

    def openDashboard_(self, sender):
        self.on_open()

    def checkForUpdates_(self, sender):
        self.on_check_updates()

    def quitApp_(self, sender):
        self.on_quit()

    def chooseProfile_(self, sender):
        self.on_profile(fan_control.PROFILE_IDS[sender.tag()])

    def allowFans_(self, sender):
        self.on_allow_fans()

    def menuNeedsUpdate_(self, menu):
        self.on_menu_needs_update()

    def menuWillOpen_(self, menu):
        self.on_menu_open(True)

    def menuDidClose_(self, menu):
        self.on_menu_open(False)


class _View(NSView):
    """A flipped container whose `relayout` (a Python callable) runs when its width changes."""

    def isFlipped(self):
        return True

    def resizeSubviewsWithOldSize_(self, old_size):
        relayout = getattr(self, "relayout", None)
        if relayout is not None:
            relayout()


def _label(font, color):
    field = NSTextField.labelWithString_("")
    field.setFont_(font)
    field.setTextColor_(color)
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    return field


def _view_item(height):
    view = _View.alloc().initWithFrame_(((0, 0), (MENU_WIDTH, height)))
    view.setAutoresizingMask_(AppKit.NSViewWidthSizable)
    item = NSMenuItem.alloc().init()
    item.setView_(view)
    return item, view


def _tile_box(fill):
    """The rounded, filled box behind a tile, as in the Thermals tiles."""
    box = NSBox.alloc().init()
    box.setBoxType_(AppKit.NSBoxCustom)
    box.setBorderWidth_(0)
    box.setCornerRadius_(6)
    box.setFillColor_(fill)
    box.setTitlePosition_(AppKit.NSNoTitle)
    return box


def _section_header(title):
    if hasattr(NSMenuItem, "sectionHeaderWithTitle_"):  # macOS 14 and later
        return NSMenuItem.sectionHeaderWithTitle_(title)
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    item.setEnabled_(False)
    return item


class StatusBar:
    def __init__(self, on_open, on_check_updates, on_quit, on_menu_open, fan_status=lambda: {},
                 on_profile=lambda pid: None, on_allow_fans=lambda: None):
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self._button = self._item.button()
        size = NSFont.menuBarFontOfSize_(0).pointSize()
        self._button.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(size, AppKit.NSFontWeightRegular))
        self._button.setImagePosition_(AppKit.NSImageLeading)
        self._title = format_watts(None)
        self._button.setTitle_(self._title)
        self._button.setToolTip_("Mac Engine Room")

        self._bolt = self._symbol("bolt.fill", size - 2, "Charging")
        self._showing_bolt = False

        self._target = _MenuTarget.alloc().init()
        self._target.on_open = on_open
        self._target.on_check_updates = on_check_updates
        self._target.on_quit = on_quit
        self._target.on_menu_needs_update = self._menu_needs_update
        self._target.on_menu_open = self._set_menu_open
        self._target.on_profile = self._choose_profile
        self._target.on_allow_fans = on_allow_fans
        self._on_menu_open = on_menu_open
        self._fan_status = fan_status
        self._on_profile = on_profile
        self._tile_fill = (NSColor.quaternarySystemFillColor() if hasattr(NSColor, "quaternarySystemFillColor")
                           else NSColor.quaternaryLabelColor())
        self._sample = None  # the latest, for filling in the rows when the menu opens
        self._thermals = None  # likewise
        self._settings = {}
        self._available = TILES  # the tiles this Mac has, once a reading says
        self._shown = {}  # what each view shows, so only changes reach AppKit
        self._clicked = None  # when the click that is opening the menu happened (time.monotonic())
        self.menu_open = False

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._target)
        self._thermals_header = _section_header("Thermals")
        menu.addItem_(self._thermals_header)
        self._build_profiles(menu)
        self._allow_fans_item = self._action_item("Allow Fan Control in Login Items…", "allowFans:", "")
        menu.addItem_(self._allow_fans_item)
        self._fans_unavailable_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Fan control isn't available", None, "")
        self._fans_unavailable_item.setEnabled_(False)
        menu.addItem_(self._fans_unavailable_item)
        self._build_tiles(menu)
        menu.addItem_(_section_header("Power"))
        self._build_rows(menu)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._action_item("Open Dashboard", "openDashboard:", "d"))
        menu.addItem_(NSMenuItem.separatorItem())
        self._update_item = self._action_item("Check for Updates…", "checkForUpdates:", "")
        menu.addItem_(self._update_item)
        menu.addItem_(self._action_item("Quit Mac Engine Room", "quitApp:", "q"))
        self._item.setMenu_(menu)
        self._menu = menu

    def remove(self):
        """Takes the item out of the menu bar, as the app quits: it's gone at once, while the app
        finishes quitting."""
        NSStatusBar.systemStatusBar().removeStatusItem_(self._item)

    @staticmethod
    def _symbol(name, point_size, description=None):
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, description)
        if image is None:
            return None
        config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(point_size, AppKit.NSFontWeightRegular)
        image = image.imageWithSymbolConfiguration_(config)
        image.setTemplate_(True)
        return image

    def _action_item(self, title, selector, key):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, key)
        item.setTarget_(self._target)
        return item

    # ── views ──

    def _build_tiles(self, menu):
        self._tiles_item, self._tiles_view = _view_item(TILE_HEIGHT)
        label_font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize() - 0.5)
        value_font = NSFont.monospacedDigitSystemFontOfSize_weight_(15, AppKit.NSFontWeightMedium)
        self._tiles = {}
        for key in TILES:
            box = _tile_box(self._tile_fill)
            label = _label(label_font, NSColor.secondaryLabelColor())
            label.setStringValue_(TILE_LABELS[key])
            value = _label(value_font, NSColor.labelColor())
            value.setStringValue_("—")
            for view in (box, label, value):
                self._tiles_view.addSubview_(view)
            self._tiles[key] = (box, label, value)
        self._tiles_view.relayout = self._layout_tiles
        self._layout_tiles()
        menu.addItem_(self._tiles_item)

    def _layout_tiles(self):
        width = self._tiles_view.frame().size.width
        shown = [k for k in TILES if k in self._available]
        tile_width = (width - 2 * INSET - (TILE_COLUMNS - 1) * TILE_GAP) / TILE_COLUMNS
        for key, (box, label, value) in self._tiles.items():
            for view in (box, label, value):
                view.setHidden_(key not in shown)
        for i, key in enumerate(shown):
            box, label, value = self._tiles[key]
            x = INSET + i % TILE_COLUMNS * (tile_width + TILE_GAP)
            y = 2 + i // TILE_COLUMNS * (TILE_HEIGHT + TILE_GAP)
            box.setFrame_(((x, y), (tile_width, TILE_HEIGHT)))
            label.setFrame_(((x + 8, y + 5), (tile_width - 12, 14)))
            value.setFrame_(((x + 8, y + 19), (tile_width - 12, 19)))
        rows = max(1, math.ceil(len(shown) / TILE_COLUMNS))
        height = 2 + rows * TILE_HEIGHT + (rows - 1) * TILE_GAP + 6
        if abs(self._tiles_view.frame().size.height - height) > 0.5:
            self._tiles_view.setFrameSize_((width, height))

    def _build_profiles(self, menu):
        """Three fan profiles in a row, the ones chosen in Settings, as tiles like the
        temperatures', the chosen one in the accent colour: a borderless button over a box,
        so it still clicks and reads as a button."""
        self._profiles_item, self._profiles_view = _view_item(PROFILE_HEIGHT + TILE_GAP)
        font = NSFont.systemFontOfSize_weight_(NSFont.smallSystemFontSize(), AppKit.NSFontWeightMedium)
        self._profile_tiles = []
        for pid in fan_control.MENU_PROFILES:  # filled in from the setting as the menu opens
            box = _tile_box(self._tile_fill)
            button = NSButton.buttonWithTitle_target_action_(fan_control.PROFILE_NAMES[pid], self._target, "chooseProfile:")
            button.setBordered_(False)
            button.setButtonType_(AppKit.NSButtonTypeMomentaryChange)
            button.setFont_(font)
            button.setTag_(fan_control.PROFILE_IDS.index(pid))
            for view in (box, button):
                self._profiles_view.addSubview_(view)
            self._profile_tiles.append((box, button))
        self._profiles_view.relayout = self._layout_profiles
        self._layout_profiles()
        menu.addItem_(self._profiles_item)

    def _layout_profiles(self):
        width = self._profiles_view.frame().size.width
        tile_width = (width - 2 * INSET - (TILE_COLUMNS - 1) * TILE_GAP) / TILE_COLUMNS
        for i, (box, button) in enumerate(self._profile_tiles):
            x = INSET + i * (tile_width + TILE_GAP)
            box.setFrame_(((x, 2), (tile_width, PROFILE_HEIGHT)))
            button.setFrame_(((x, 2), (tile_width, PROFILE_HEIGHT)))

    def _choose_profile(self, pid):
        self._fill_profiles(pid)  # at once, not at the next sample
        self._on_profile(pid)

    def _build_rows(self, menu):
        font = NSFont.menuFontOfSize_(0)
        value_font = NSFont.monospacedDigitSystemFontOfSize_weight_(font.pointSize(), AppKit.NSFontWeightRegular)
        self._rows = {}
        self._row_views = {}
        for key in ROWS:
            item, view = _view_item(ROW_HEIGHT)
            icon = NSImageView.alloc().initWithFrame_(((INSET, 3), (18, 16)))
            icon.setContentTintColor_(NSColor.secondaryLabelColor())
            label = _label(font, NSColor.secondaryLabelColor())
            label.setStringValue_(ROW_LABELS[key])
            value = _label(value_font, NSColor.labelColor())
            value.setAlignment_(AppKit.NSTextAlignmentRight)
            value.setStringValue_("—")
            for v in (icon, label, value):
                view.addSubview_(v)
            view.relayout = lambda key=key: self._layout_row(key)
            menu.addItem_(item)
            self._rows[key] = item
            self._row_views[key] = (view, icon, label, value)
            self._set_symbol(key, ROW_SYMBOLS.get(key, "battery.100percent"))
            self._layout_row(key)

    def _layout_row(self, key):
        """The value gets the width it needs; the label (an adapter's name can be long) the rest."""
        view, icon, label, value = self._row_views[key]
        width = view.frame().size.width
        value_width = min(math.ceil(value.fittingSize().width), width / 2)
        height = math.ceil(label.fittingSize().height)
        y = (ROW_HEIGHT - height) / 2
        value.setFrame_(((width - INSET - value_width, y), (value_width, height)))
        label.setFrame_(((INSET + 24, y), (width - 2 * INSET - 24 - value_width - 8, height)))

    def _put(self, view, setter, value):
        """Pass `value` to AppKit only if it isn't already showing. Returns True if it was new."""
        key = (id(view), setter)
        if self._shown.get(key) == value:
            return False
        self._shown[key] = value
        getattr(view, setter)(value)
        return True

    def _set_symbol(self, key, name):
        if self._shown.get((key, "symbol")) != name:
            self._shown[(key, "symbol")] = name
            self._row_views[key][1].setImage_(self._symbol(name, 13))

    def _set_row(self, key, value, label=None):
        _, _, label_field, value_field = self._row_views[key]
        changed = self._put(value_field, "setStringValue_", value)
        changed = self._put(label_field, "setStringValue_", label or ROW_LABELS[key]) or changed
        if changed:
            self._layout_row(key)

    # ── updates ──

    def set_update_status(self, status, version):
        """Mirrors updater.Updater's status: only "check" and "restart" can be clicked."""
        title, enabled = {
            "checking": ("Checking for Updates…", False),
            "downloading": (f"Downloading Mac Engine Room {version}…", False),
            "ready": (f"Restart to Update to {version}", True),
        }.get(status, ("Check for Updates…", True))
        self._update_item.setTitle_(title)
        self._update_item.setEnabled_(enabled)

    def update(self, sample, thermals, settings):
        """Called for every sample, with `thermals` when temperatures were read for it. A new
        title costs a redraw (about 0.9 ms, measured) and even setting the same one again
        costs 0.2 ms, so only changes go through. The menu's rows are only filled in
        while it's open."""
        charging_title = bool(settings.get("menu_bar_show_charging") and sample.get("is_charging"))
        title = menu_bar_title(sample, thermals, settings)
        if title != self._title:
            self._button.setTitle_(title)
            self._title = title
        if charging_title != self._showing_bolt and self._bolt:
            self._button.setImage_(self._bolt if charging_title else None)
            self._showing_bolt = charging_title

        self._sample = sample
        self._settings = settings
        if thermals is not None:
            self._thermals = thermals
        if self.menu_open:
            self._fill()

    def _menu_needs_update(self):
        """AppKit's first word that the menu is about to open."""
        self._clicked = self._click_time()
        self._fill()

    def _click_time(self):
        """When the click on this item that is opening the menu happened, or None if the menu
        isn't opening from one (the keyboard, accessibility). NSEvent counts seconds since boot,
        awake, which is time.monotonic()'s clock (mach_absolute_time)."""
        try:
            event = AppKit.NSApp.currentEvent()
            if event is None or event.type() not in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeRightMouseDown):
                return None
            if event.window() != self._button.window():
                return None
            age = time.monotonic() - event.timestamp()
            return event.timestamp() if 0 <= age < 60 else None
        except Exception:
            return None

    def _set_menu_open(self, is_open):
        self.menu_open = is_open
        self._on_menu_open()
        if is_open and self._clicked is not None:
            note = slow_menu_note(time.monotonic() - self._clicked)
            if note:
                log.info(note)
        self._clicked = None

    def _fill(self):
        self._fill_profiles()
        self._fill_tiles()
        sample = self._sample
        if sample is None:
            return
        self._set_row("battery", battery_status(sample))
        self._set_symbol("battery", battery_symbol(sample))
        self._set_symbol("system", "laptopcomputer" if sample.get("has_battery") else "desktopcomputer")
        self._set_row("system", format_watts(sample.get("system_load"), pad_to=0))
        self._set_row("screen", format_watts(sample.get("screen_power"), pad_to=0))
        self._set_row("chip", format_watts(sample.get("chip_power"), pad_to=0))
        adapter = sample.get("adapter")
        if sample.get("external_connected") and adapter:
            name = adapter.get("name") or "Adapter"  # power.adapter_name: "140W USB-C Power Adapter", any brand
            self._set_row("adapter", format_watts(sample.get("adapter_power"), pad_to=0), label=name)
        else:
            self._set_row("adapter", "Not connected")
        self._put(self._rows["battery"], "setHidden_", not sample.get("has_battery"))
        self._put(self._rows["adapter"], "setHidden_", not sample.get("has_battery"))  # a desktop's is its mains
        self._put(self._rows["screen"], "setHidden_", not sample.get("has_screen_power"))
        self._put(self._rows["chip"], "setHidden_", not sample.get("has_chip_power"))

    def _fill_profiles(self, chosen=None):
        status = self._fan_status() or {}
        state = status.get("state", "nofans")
        has_fans = "fans" in self._available and state != "nofans"
        chosen = chosen or self._settings.get("fan_profile") or status.get("profile") or fan_control.MACOS
        usable = state in PROFILES_USABLE
        self._put(self._profiles_item, "setHidden_", not has_fans)
        # If the profile in use isn't one of the three (chosen on the Thermals page), none is lit.
        shown = self._settings.get("menu_fan_profiles") or fan_control.MENU_PROFILES
        for pid, (box, button) in zip(shown, self._profile_tiles):
            self._put(button, "setTitle_", fan_control.PROFILE_NAMES[pid])
            self._put(button, "setTag_", fan_control.PROFILE_IDS.index(pid))
            on = pid == chosen
            self._put(box, "setFillColor_", NSColor.controlAccentColor() if on else self._tile_fill)
            self._put(button, "setContentTintColor_", NSColor.whiteColor() if on else NSColor.labelColor())
            self._put(button, "setAccessibilityValue_", "selected" if on else "")
            self._put(button, "setEnabled_", usable)
        self._put(self._allow_fans_item, "setHidden_", not (has_fans and state == "approval"))
        self._put(self._fans_unavailable_item, "setHidden_", not (has_fans and state == "unavailable"))

    def _fill_tiles(self):
        thermals = self._thermals
        if thermals is not None and tuple(thermals["available"]) != self._available:
            self._available = tuple(thermals["available"])  # this Mac's sensors: never stale
            self._layout_tiles()
        if thermals is not None and time.time() - thermals["t"] > THERMALS_FRESH_FOR:
            thermals = None  # from before the menu opened: a fresh reading is on its way
        none = not self._available
        self._put(self._thermals_header, "setHidden_", none)
        self._put(self._tiles_item, "setHidden_", none)
        thermals = thermals or {}
        for key in self._available:
            if key == "fans":
                fans = thermals.get("fans") or []
                self._put(self._tiles[key][1], "setStringValue_", "Fan · rpm" if len(fans) == 1 else TILE_LABELS[key])
                text = fans_text(fans or [{"rpm": None}])
            else:
                # A parked GPU shows its last real reading, dimmed once it's a minute old.
                held = thermals.get("gpu_held") if key == "gpu" and thermals.get("gpu") is None else None
                old = bool(held) and time.time() - held["t"] >= HELD_NOTE_AFTER
                text = format_temperature(held["c"] if held else thermals.get(key), uses_fahrenheit(self._settings))
                if key == "gpu":
                    self._put(self._tiles[key][2], "setTextColor_",
                              NSColor.secondaryLabelColor() if old else NSColor.labelColor())
            self._put(self._tiles[key][2], "setStringValue_", text)
