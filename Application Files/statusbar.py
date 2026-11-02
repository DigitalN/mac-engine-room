"""Menu bar item: live wattage in the title, current readings in its menu.

A plain NSStatusItem with a native NSMenu. The Rust app used a Tauri tray icon
plus an NSPopover, and on macOS 27 AppKit swallowed the clicks so the panel
never opened; a standard status-item menu has no such dependency.

Every method here must run on the main thread (main.py dispatches with
AppHelper.callAfter).
"""

import AppKit
from AppKit import (
    NSAttributedString, NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
    NSImage, NSImageSymbolConfiguration, NSMenu, NSMenuItem, NSMutableParagraphStyle,
    NSParagraphStyleAttributeName, NSStatusBar, NSTextTab, NSVariableStatusItemLength,
)
from Foundation import NSObject

FIGURE_SPACE = " "  # same width as a digit in a monospaced-digit font
ROWS = ("battery", "system", "screen", "chip", "adapter")
ROW_LABELS = {"battery": "Battery", "system": "System", "screen": "Screen",
              "chip": "Chip (CPU + GPU)", "adapter": "Adapter"}
VALUE_TAB_LOCATION = 250.0


def format_watts(value, pad_to=4):
    """'27.1 W', padded with figure spaces so the menu bar width stays put."""
    if not isinstance(value, (int, float)):
        return "— W"
    return f"{value:.1f}".rjust(pad_to, FIGURE_SPACE) + " W"


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


def menu_bar_value(sample, settings):
    """The number shown in the menu bar for the user's chosen metric."""
    if settings.get("menu_bar_show_charging") and sample.get("is_charging"):
        return sample.get("system_in")
    metric = settings.get("menu_bar_metric")
    if metric == "screen" and not sample.get("has_screen_power"):
        metric = "system"  # a Mac with no screen sensor doesn't offer it; the setting stays as it was
    return {
        "system": sample.get("system_load"),
        "screen": sample.get("screen_power"),
        "chip": sample.get("chip_power"),
    }.get(metric, sample.get("system_load"))


class _MenuTarget(NSObject):
    """Receives menu item actions and the menu's delegate calls, and forwards them to
    Python callbacks."""

    def openDashboard_(self, sender):
        self.on_open()

    def checkForUpdates_(self, sender):
        self.on_check_updates()

    def quitApp_(self, sender):
        self.on_quit()

    def menuNeedsUpdate_(self, menu):
        self.on_menu_needs_update()

    def menuWillOpen_(self, menu):
        self.on_menu_open(True)

    def menuDidClose_(self, menu):
        self.on_menu_open(False)


class StatusBar:
    def __init__(self, on_open, on_check_updates, on_quit, on_menu_open):
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self._button = self._item.button()
        size = NSFont.menuBarFontOfSize_(0).pointSize()
        self._button.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(size, AppKit.NSFontWeightRegular))
        self._button.setImagePosition_(AppKit.NSImageLeading)
        self._title = format_watts(None)
        self._button.setTitle_(self._title)
        self._button.setToolTip_("Mac Engine Room")

        bolt = NSImage.imageWithSystemSymbolName_accessibilityDescription_("bolt.fill", "Charging")
        config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size - 2, AppKit.NSFontWeightRegular)
        self._bolt = bolt.imageWithSymbolConfiguration_(config) if bolt else None
        if self._bolt:
            self._bolt.setTemplate_(True)
        self._showing_bolt = False

        self._target = _MenuTarget.alloc().init()
        self._target.on_open = on_open
        self._target.on_check_updates = on_check_updates
        self._target.on_quit = on_quit
        self._target.on_menu_needs_update = self._fill_rows
        self._target.on_menu_open = self._set_menu_open
        self._on_menu_open = on_menu_open
        self._sample = None  # the latest, for filling in the rows when the menu opens
        self.menu_open = False

        self._menu_font = NSFont.menuFontOfSize_(0)
        self._value_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
            self._menu_font.pointSize(), AppKit.NSFontWeightRegular)
        style = NSMutableParagraphStyle.alloc().init()
        style.setTabStops_([NSTextTab.alloc().initWithTextAlignment_location_options_(
            AppKit.NSTextAlignmentRight, VALUE_TAB_LOCATION, {})])
        self._paragraph = style

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._target)
        self._rows = {}
        self._row_values = {}
        for key in ROWS:
            row = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(ROW_LABELS[key], None, "")
            row.setEnabled_(False)
            menu.addItem_(row)
            self._rows[key] = row
            self._set_row(key, "—")
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._action_item("Open Dashboard", "openDashboard:", "d"))
        menu.addItem_(NSMenuItem.separatorItem())
        self._update_item = self._action_item("Check for Updates…", "checkForUpdates:", "")
        menu.addItem_(self._update_item)
        menu.addItem_(self._action_item("Quit Mac Engine Room", "quitApp:", "q"))
        self._item.setMenu_(menu)
        self._menu = menu

    def _action_item(self, title, selector, key):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, key)
        item.setTarget_(self._target)
        return item

    def _set_row(self, key, value):
        if self._row_values.get(key) == value:
            return
        self._row_values[key] = value
        text = f"{ROW_LABELS[key]}\t{value}"
        title = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(text, {
            NSFontAttributeName: self._menu_font,
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
            NSParagraphStyleAttributeName: self._paragraph,
        })
        start = len(ROW_LABELS[key]) + 1
        title.addAttributes_range_({
            NSFontAttributeName: self._value_font,
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }, (start, len(text) - start))
        self._rows[key].setAttributedTitle_(title)

    def set_update_status(self, status, version):
        """Mirrors updater.Updater's status: only "check" and "restart" can be clicked."""
        title, enabled = {
            "checking": ("Checking for Updates…", False),
            "downloading": (f"Downloading Mac Engine Room {version}…", False),
            "ready": (f"Restart to Update to {version}", True),
        }.get(status, ("Check for Updates…", True))
        self._update_item.setTitle_(title)
        self._update_item.setEnabled_(enabled)

    def update(self, sample, settings):
        """Called for every sample. A new title costs a redraw (about 0.9 ms, measured)
        and even setting the same one again costs 0.2 ms, so only changes go through.
        The menu's rows are only filled in while it's open."""
        charging_title = bool(settings.get("menu_bar_show_charging") and sample.get("is_charging"))
        title = format_watts(menu_bar_value(sample, settings))
        if title != self._title:
            self._button.setTitle_(title)
            self._title = title
        if charging_title != self._showing_bolt and self._bolt:
            self._button.setImage_(self._bolt if charging_title else None)
            self._showing_bolt = charging_title

        self._sample = sample
        if self.menu_open:
            self._fill_rows()

    def _set_menu_open(self, is_open):
        self.menu_open = is_open
        self._on_menu_open()

    def _fill_rows(self):
        sample = self._sample
        if sample is None:
            return
        self._set_row("battery", battery_status(sample))
        self._set_row("system", format_watts(sample.get("system_load"), pad_to=0))
        self._set_row("screen", format_watts(sample.get("screen_power"), pad_to=0))
        self._set_row("chip", format_watts(sample.get("chip_power"), pad_to=0))
        adapter = sample.get("adapter")
        if sample.get("external_connected") and adapter:
            name = adapter.get("name") or (f"{adapter['watts']}W adapter" if adapter.get("watts") else "Adapter")
            self._set_row("adapter", f"{name} · {format_watts(sample.get('adapter_power'), pad_to=0)}")
        else:
            self._set_row("adapter", "Not connected")
        self._rows["battery"].setHidden_(not sample.get("has_battery"))
        self._rows["screen"].setHidden_(not sample.get("has_screen_power"))
