"""Settings validation, persistence and migration from the Rust app's preferences."""

import json

from settings import DEFAULTS, Settings, from_legacy_preferences, validate


def test_validate_fills_defaults_and_drops_junk():
    assert validate(None) == DEFAULTS
    s = validate({"menu_bar_metric": "none", "theme": "neon", "update_interval_ms": 500,
                  "record_history": "yes", "unknown": 1})
    assert s == DEFAULTS


def test_legacy_preferences_are_imported(tmp_path):
    legacy = tmp_path / "preference.json"
    legacy.write_text(json.dumps({"statusBarItem": "screen", "statusBarShowCharging": False,
                                  "language": "en", "theme": "dark", "updateInterval": 500}))
    s = Settings(tmp_path / "settings.json", legacy_path=legacy).get()
    assert (s["menu_bar_metric"], s["menu_bar_show_charging"], s["theme"]) == ("screen", False, "dark")
    assert json.loads((tmp_path / "settings.json").read_text()) == s


def test_legacy_heatpipe_metric_becomes_chip():
    assert from_legacy_preferences({"statusBarItem": "heatpipe"})["menu_bar_metric"] == "chip"
    assert validate({"menu_bar_metric": "chip"})["menu_bar_metric"] == "chip"


def test_bad_legacy_value_falls_back_to_default():
    mapped = from_legacy_preferences({"statusBarItem": "none", "updateInterval": 86_400_000})
    s = validate({**DEFAULTS, **{k: v for k, v in mapped.items() if v is not None}})
    assert s == DEFAULTS


def test_update_persists_and_notifies_only_on_change(tmp_path):
    store = Settings(tmp_path / "settings.json")
    seen = []
    store.on_change(seen.append)
    store.update({"theme": "light"})
    store.update({"theme": "light"})
    store.update({"record_history": False})
    assert [v["theme"] for v in seen] == ["light", "light"] and seen[-1]["record_history"] is False
    assert Settings(tmp_path / "settings.json").get()["theme"] == "light"


def test_corrupt_file_uses_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    assert Settings(tmp_path / "settings.json").get() == DEFAULTS


def test_automatic_updates_start_on_and_can_be_turned_off(tmp_path):
    store = Settings(tmp_path / "settings.json")
    assert store.get()["update_automatically"] is True
    store.update({"update_automatically": False})
    assert Settings(tmp_path / "settings.json").get()["update_automatically"] is False
    assert validate({"update_automatically": "no"})["update_automatically"] is True


def test_menu_bar_can_show_a_temperature_and_watts_stay_the_default():
    assert DEFAULTS["menu_bar_metric"] == "system"
    assert validate({"menu_bar_metric": "cpu_temp"})["menu_bar_metric"] == "cpu_temp"
    assert validate({"menu_bar_metric": "hottest_temp"})["menu_bar_metric"] == "hottest_temp"
    assert validate({"menu_bar_metric": "gpu_temp"})["menu_bar_metric"] == "system"


def test_temperatures_are_in_celsius_unless_fahrenheit_is_chosen():
    assert DEFAULTS["temperature_unit"] == "celsius"
    assert validate({"temperature_unit": "fahrenheit"})["temperature_unit"] == "fahrenheit"
    assert validate({"temperature_unit": "kelvin"})["temperature_unit"] == "celsius"
