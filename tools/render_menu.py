"""Draw the menu bar menu to PNGs, light and dark, without showing anything on screen.

    python3 tools/render_menu.py OUT [--state holding] [--profile balanced] [--fahrenheit] ...

Writes OUT-light.png and OUT-dark.png, and prints the menu's items in order. The custom
views (fan profiles, the temperature tiles, the Power rows) are statusbar.py's
own, filled in from made-up readings; the plain items (section headers, actions) are drawn
as labels, so their fonts and spacing only approximate macOS's. The readings match
tools/preview/stub.js. `--help` lists the options.
"""

import argparse
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Application Files"))

import AppKit  # noqa: E402
from AppKit import NSAppearance, NSBox, NSColor, NSFont  # noqa: E402

import fans  # noqa: E402
import settings as app_settings  # noqa: E402
import statusbar  # noqa: E402

STATES = ("off", "holding", "override", "approval", "unavailable", "other", "error")


def options():
    a = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    a.add_argument("out", help="path prefix for the PNGs")
    a.add_argument("--state", choices=STATES, default="off", help="fan control's state (default off)")
    a.add_argument("--profile", choices=fans.PROFILE_IDS, help="the chosen profile (default: macos when off, else balanced)")
    a.add_argument("--menu-profiles", default=",".join(fans.MENU_PROFILES), help="the three profiles in the menu")
    a.add_argument("--fahrenheit", action="store_true")
    a.add_argument("--fanless", action="store_true", help="a Mac with no fans")
    a.add_argument("--onefan", action="store_true")
    a.add_argument("--noscreen", action="store_true", help="a Mac with no screen power reading")
    a.add_argument("--nochip", action="store_true", help="a Mac with no chip power reading (with --noscreen: an M4 Air without IOReport)")
    a.add_argument("--desktop", action="store_true", help="a Mac mini: no battery, no screen")
    a.add_argument("--gpuheld", action="store_true", help="the GPU parked for 3 minutes, showing its last reading")
    a.add_argument("--charging", action="store_true")
    a.add_argument("--hot", type=float, default=0, help="degrees added to every temperature")
    return a.parse_args()


def readings(o):
    limits = [(1499, 4296), (1499, 4744)][: 0 if o.fanless else 1 if o.onefan else 2]
    profile = o.profile or ("macos" if o.state == "off" else "balanced")
    held = o.state in ("holding", "override", "other")
    targets = ([hi for _, hi in limits] if o.state == "override" or profile == "max" else
               [2310, 2560][: len(limits)])
    status = {"profile": profile, "state": "nofans" if o.fanless else o.state}
    status.update({
        "holding": {"targets": targets},
        "override": {"reason": "pressure", "pressure": 2, "targets": targets},
        "unavailable": {"reason": "until Mac Engine Room is in your Applications folder"},
        "error": {"reason": "it couldn't reach the fan helper"},
    }.get(o.state, {}))
    h = o.hot
    thermals = {
        "t": time.time(), "cpu": 52.4 + h, "gpu": 48.1 + h, "memory": 47.0 + h * 0.8, "ssd": 35.5 + h * 0.4,
        "ambient": 26.3 + h * 0.2, "hottest": 52.4 + h,
        "available": [k for k in statusbar.TILES if not (o.fanless and k == "fans")],
        "fans": [{"rpm": targets[i] if held else 0, "min": lo, "max": hi, "manual": held} for i, (lo, hi) in enumerate(limits)],
    }
    if o.gpuheld:
        thermals.update(gpu=None, gpu_held={"c": 47.9 + h, "t": thermals["t"] - 180, "sensors": 2})
    sample = {
        "has_battery": True, "battery_level": 63, "charge_limit": 80, "is_charging": o.charging,
        "external_connected": o.charging, "time_remaining_min": 64 if o.charging else 254,
        "system_load": 9.4, "system_in": 52.3 if o.charging else 0, "screen_power": None if o.noscreen else 2.3,
        "has_screen_power": not o.noscreen, "chip_power": None if o.nochip else 1.8, "has_chip_power": not o.nochip,
        "adapter_power": 52.3 if o.charging else 0,
        "adapter": {"name": "96W USB-C Power Adapter", "watts": 96} if o.charging else None,
    }
    if o.desktop:
        sample.update(has_battery=False, external_connected=True, screen_power=None, has_screen_power=False,
                      system_in=10.3, adapter=None, battery_level=None, time_remaining_min=None)
    settings = {**app_settings.DEFAULTS, "fan_profile": profile,
                "temperature_unit": "fahrenheit" if o.fahrenheit else "celsius",
                "menu_fan_profiles": o.menu_profiles.split(",")}
    return status, sample, thermals, settings


def build_menu(o):
    """statusbar.StatusBar filled in as if its menu had just opened, with no status item."""
    status, sample, thermals, settings = readings(o)
    button = SimpleNamespace(**{n: (lambda *a: None) for n in
                                ("setFont_", "setImagePosition_", "setTitle_", "setToolTip_", "setImage_")})
    item = SimpleNamespace(button=lambda: button, setMenu_=lambda m: None)
    statusbar.NSStatusBar = SimpleNamespace(systemStatusBar=lambda: SimpleNamespace(statusItemWithLength_=lambda l: item))
    bar = statusbar.StatusBar(on_open=None, on_check_updates=None, on_quit=None, on_menu_open=lambda: None,
                              fan_status=lambda: status)
    bar.update(sample, thermals, settings)
    bar._target.menuNeedsUpdate_(bar._menu)
    return bar


def label(item, width):
    """A plain item's title as a section header, a disabled item or an action would show it."""
    header = getattr(item, "isSectionHeader", lambda: False)()
    field = AppKit.NSTextField.labelWithString_(item.title())
    field.setFont_(NSFont.boldSystemFontOfSize_(NSFont.smallSystemFontSize()) if header else NSFont.menuFontOfSize_(0))
    field.setTextColor_(NSColor.labelColor() if item.isEnabled() and not header else NSColor.secondaryLabelColor())
    field.setFrame_(((statusbar.INSET, 3), (width - 2 * statusbar.INSET, 16)))
    holder = statusbar._View.alloc().initWithFrame_(((0, 0), (width, statusbar.ROW_HEIGHT)))
    holder.addSubview_(field)
    return holder


def rows(bar, width):
    """(name, view) for each item showing, top to bottom."""
    names = [(bar._profiles_item, "fan profiles"), (bar._tiles_item, "temperature tiles")] + [(item, f"{key} row") for key, item in bar._rows.items()]
    out = []
    for item in bar._menu.itemArray():
        if item.isHidden():
            continue
        if item.view() is not None:
            name = next((n for i, n in names if i == item), "view")
            out.append((f"[{name}, {round(item.view().frame().size.height)} pt]", item.view()))
        elif item.isSeparatorItem():
            line = NSBox.alloc().initWithFrame_(((statusbar.INSET, 5), (width - 2 * statusbar.INSET, 1)))
            line.setBoxType_(AppKit.NSBoxSeparator)
            holder = statusbar._View.alloc().initWithFrame_(((0, 0), (width, 11)))
            holder.addSubview_(line)
            out.append(("---", holder))
        else:
            out.append((item.title(), label(item, width)))
    return out


def render(bar, out):
    width = statusbar.MENU_WIDTH
    items = rows(bar, width)
    for name, _ in items:
        print(name)
    height = sum(v.frame().size.height for _, v in items) + 10
    for name, appearance, bg in (("light", AppKit.NSAppearanceNameAqua, (0.93, 0.93, 0.94)),
                                 ("dark", AppKit.NSAppearanceNameDarkAqua, (0.17, 0.17, 0.18))):
        NSAppearance.setCurrentAppearance_(NSAppearance.appearanceNamed_(appearance))
        root = statusbar._View.alloc().initWithFrame_(((0, 0), (width, height)))
        root.setAppearance_(NSAppearance.appearanceNamed_(appearance))
        back = statusbar._tile_box(NSColor.colorWithSRGBRed_green_blue_alpha_(*bg, 1.0))
        back.setCornerRadius_(0)
        back.setFrame_(((0, 0), (width, height)))
        root.addSubview_(back)
        y = 5
        for _, v in items:
            # Each view in a holder of its own: moved straight into root, the width-sizable
            # ones were stretched by root's autoresizing.
            h = v.frame().size.height
            v.removeFromSuperview()
            v.setAutoresizingMask_(0)
            holder = statusbar._View.alloc().initWithFrame_(((0, y), (width, h)))
            root.addSubview_(holder)
            holder.addSubview_(v)
            v.setFrame_(((0, 0), (width, h)))
            y += h
        rep = root.bitmapImageRepForCachingDisplayInRect_(root.bounds())
        root.cacheDisplayInRect_toBitmapImageRep_(root.bounds(), rep)
        path = f"{out}-{name}.png"
        rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(path, True)
        print(path)


if __name__ == "__main__":
    o = options()
    render(build_menu(o), o.out)
