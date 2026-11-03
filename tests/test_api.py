"""Methods the dashboard page calls (no window or app is created)."""

from types import SimpleNamespace

from PyObjCTools import AppHelper

from main import CHARGING_SETTINGS_URL, LOG_PATH, Api, LiveState


def test_log_file_button_shows_the_log_file(monkeypatch):
    calls = []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *args: calls.append((func, args)))
    Api(app=None).reveal_log_file()
    [(func, ([url],))] = calls
    assert func.__name__ == "activateFileViewerSelectingURLs_"
    assert url.path() == str(LOG_PATH)


def test_charge_limit_button_opens_charging(monkeypatch):
    calls = []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *args: calls.append((func, args)))
    Api(app=None).open_charging_settings()
    [(func, (url,))] = calls
    assert func.__name__ == "openURL_"
    assert url is not None and url.absoluteString() == CHARGING_SETTINGS_URL
    assert url.scheme() == "x-apple.systempreferences"
    assert url.absoluteString().endswith("?charging")


def test_check_for_updates_button_does_what_the_menu_item_does(monkeypatch):
    calls = []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *args: calls.append((func, args)))
    updater = SimpleNamespace(check_now=lambda: None)
    Api(app=SimpleNamespace(_updater=updater)).check_for_updates()
    assert calls == [(updater.check_now, ())]


def test_live_data_carries_the_update_status_for_settings():
    app = SimpleNamespace(_updater=SimpleNamespace(status="downloading", version="1.2.5"),
                          _sampler=SimpleNamespace(dashboard_polled=lambda fans: None), _live=LiveState(),
                          _fans=SimpleNamespace(status={"profile": "balanced", "state": "holding", "targets": [1499, 1499]}))
    live = Api(app=app).get_live(0)
    assert live["update"] == ["downloading", "1.2.5"]
    assert live["fan_control"] == {"profile": "balanced", "state": "holding", "targets": [1499, 1499]}


def test_fans_are_read_while_the_dashboard_or_thermals_is_showing():
    polls = []
    app = SimpleNamespace(_updater=SimpleNamespace(status="idle", version=None),
                          _sampler=SimpleNamespace(dashboard_polled=lambda fans: polls.append(fans)),
                          _live=LiveState(), _fans=SimpleNamespace(status={}))
    for page in ("dashboard", "thermals", "now", "settings"):
        Api(app=app).get_live(0, page)
    assert polls == [True, True, False, False]


def test_thermal_points_only_exist_where_temperatures_were_read():
    """Temperatures are read every sample; a failed read (None) leaves no point."""
    live = LiveState()
    power = {"t": 1.0, "system_load": 7.0, "system_in": 0.0, "battery_power": -7.0, "screen_power": 2.0,
             "chip_power": 3.0}
    thermals = {"t": 2.0, "cpu": 61.4, "gpu": None, "memory": None, "ssd": None, "ambient": None}
    live.push(power)
    live.push({**power, "t": 2.0}, thermals)
    live.push({**power, "t": 3.0})
    r = live.since(0)
    assert len(r["points"]) == 3 and r["thermal_points"] == [[2.0, 61.4, None, None, None, None]]
    assert r["thermals"] is thermals and r["thermal_fields"][1] == "cpu"
    assert live.since(2)["thermal_points"] == []
