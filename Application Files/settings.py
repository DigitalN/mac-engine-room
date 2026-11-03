"""User settings, stored as JSON in the app data folder.

Every value is validated on load and on save: a stale or hand-edited file can
never put the app into a bad state (the old app froze its chart on an unknown
`statusBarItem` value).
"""

import json
import logging
import math
import os
import threading
from pathlib import Path

import fans

log = logging.getLogger(__name__)

MENU_BAR_METRICS = ("system", "screen", "chip", "cpu_temp", "hottest_temp")
THEMES = ("system", "light", "dark")
TEMPERATURE_UNITS = ("celsius", "fahrenheit")  # Celsius unless chosen: macOS's own unit isn't followed

DEFAULTS = {
    "menu_bar_metric": "system",
    "menu_bar_show_charging": True,
    "theme": "system",
    "temperature_unit": "celsius",
    "open_dashboard_at_launch": True,
    "record_history": True,
    "update_automatically": True,
    "fan_profile": fans.MACOS,  # until a profile is chosen, the fans are macOS's
    "fan_manual_level": 0.0,  # Manual's slider: 0 is each fan's minimum speed, 1 its maximum
    "fan_tuning": {},  # {profile: {"start", "full", "top"}}, only what differs from its defaults
    "menu_fan_profiles": fans.MENU_PROFILES,  # three different profiles, in the menu's order
}


def validate(raw):
    """Return a complete, valid settings dict built from `raw` (unknown keys dropped)."""
    s = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return s
    if raw.get("menu_bar_metric") in MENU_BAR_METRICS:
        s["menu_bar_metric"] = raw["menu_bar_metric"]
    if raw.get("theme") in THEMES:
        s["theme"] = raw["theme"]
    if raw.get("temperature_unit") in TEMPERATURE_UNITS:
        s["temperature_unit"] = raw["temperature_unit"]
    for key in ("menu_bar_show_charging", "open_dashboard_at_launch", "record_history", "update_automatically"):
        if isinstance(raw.get(key), bool):
            s[key] = raw[key]
    if raw.get("fan_profile") in fans.PROFILE_IDS:
        s["fan_profile"] = raw["fan_profile"]
    level = raw.get("fan_manual_level")
    if isinstance(level, (int, float)) and not isinstance(level, bool) and math.isfinite(level):
        s["fan_manual_level"] = round(min(max(float(level), 0.0), 1.0), 3)
    s["fan_tuning"] = fans.validate_tuning(raw.get("fan_tuning"))
    shown = raw.get("menu_fan_profiles")
    valid = isinstance(shown, list) and len(shown) == 3 and len(set(shown)) == 3 and all(p in fans.PROFILE_IDS for p in shown)
    s["menu_fan_profiles"] = list(shown if valid else fans.MENU_PROFILES)
    return s


def from_legacy_preferences(legacy):
    """Map the Rust app's tauri-plugin-pinia preference.json onto our settings."""
    if not isinstance(legacy, dict):
        return {}
    metric = legacy.get("statusBarItem")
    return {
        # The Rust app called the CPU/GPU package rail (SMC PHPC) "heatpipe".
        "menu_bar_metric": "chip" if metric == "heatpipe" else metric,
        "menu_bar_show_charging": legacy.get("statusBarShowCharging"),
        "theme": legacy.get("theme"),
    }


class Settings:
    """Thread-safe settings store. `on_change` listeners get the new dict."""

    def __init__(self, path, legacy_path=None):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._listeners = []
        self._values = self._load(legacy_path)

    def _load(self, legacy_path):
        if self._path.exists():
            try:
                return validate(json.loads(self._path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.exception("Could not read %s; using defaults", self._path)
                return dict(DEFAULTS)

        values = dict(DEFAULTS)
        if legacy_path and Path(legacy_path).exists():
            try:
                legacy = json.loads(Path(legacy_path).read_text(encoding="utf-8"))
                values = validate({**DEFAULTS, **{k: v for k, v in from_legacy_preferences(legacy).items() if v is not None}})
                log.info("Imported preferences from the previous Powerflow version")
            except (OSError, ValueError):
                log.exception("Could not read legacy preferences at %s", legacy_path)
        self._write(values)
        return values

    def _write(self, values):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self):
        with self._lock:
            return dict(self._values)

    def update(self, changes):
        with self._lock:
            merged = validate({**self._values, **(changes if isinstance(changes, dict) else {})})
            changed = merged != self._values
            if changed:
                self._values = merged
                self._write(merged)
            values = dict(self._values)
        if changed:
            for listener in list(self._listeners):
                try:
                    listener(values)
                except Exception:
                    log.exception("Settings listener failed")
        return values

    def on_change(self, listener):
        self._listeners.append(listener)
