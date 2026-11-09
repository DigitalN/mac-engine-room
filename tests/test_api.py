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


def test_live_data_tells_the_page_which_power_sensors_this_mac_has():
    """has_screen_power and has_chip_power ride in `latest` (the whole sample), which is how the
    page decides what to show."""
    import power

    app = SimpleNamespace(_updater=SimpleNamespace(status="idle", version=None),
                          _sampler=SimpleNamespace(dashboard_polled=lambda fans: None),
                          _live=LiveState(), _fans=SimpleNamespace(status={}))
    for smc, screen, chip in [({"PSTR": 9.0, "PDBR": 2.3, "PHPC": 1.8}, True, True),
                              ({"PSTR": 9.0, "PBLR": 0.01, "PHPC": 0.01}, False, False)]:  # the M4 Air's
        app._live.push(power.normalize(None, smc, now=1.0))
        latest = Api(app=app).get_live(0)["latest"]
        assert latest["has_screen_power"] is screen and latest["has_chip_power"] is chip


def test_report_a_problem_opens_the_sensor_report_issue(monkeypatch):
    calls = []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *args: calls.append((func, args)))
    Api(app=None).report_problem()
    [(func, (url,))] = calls
    assert func.__name__ == "openURL_"
    assert url.absoluteString() == "https://github.com/DigitalN/mac-engine-room/issues/new?template=sensor-report.yml"


def test_copy_sensor_report_puts_it_on_the_clipboard_or_says_it_couldnt(monkeypatch):
    import main
    import report

    calls = []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *args: calls.append((func, args)))
    monkeypatch.setattr(report, "collect", lambda version, sample, source, error, counters: f"report {version} {sample} {source} {error} {counters}")
    live = LiveState()
    # Where Chip comes from IOReport, the reader says so, and the report names it.
    reader = SimpleNamespace(ioreport_source="CLPC energy counters", ioreport_error=None,
                             ioreport_counters="CPU 1.20 W, GPU 0.31 W, ANE 0.00 W")
    assert Api(app=SimpleNamespace(_live=live, _reader=reader)).copy_sensor_report() is True
    assert calls == [(main._copy_to_clipboard, (f"report {main.VERSION} None CLPC energy counters None CPU 1.20 W, GPU 0.31 W, ANE 0.00 W",))]

    def no_smc(version, sample, source, error, counters):
        raise OSError("AppleSMC service not found")
    monkeypatch.setattr(report, "collect", no_smc)
    assert Api(app=SimpleNamespace(_live=live, _reader=reader)).copy_sensor_report() is False


def test_settings_asks_for_a_report_only_when_the_app_guessed(monkeypatch):
    import main
    import power

    monkeypatch.setattr(power, "mac_model", lambda: "MacBookPro18,3")
    live = LiveState()
    app = SimpleNamespace(_thermals=SimpleNamespace(guessing=()), _live=live,
                          _reader=SimpleNamespace(has_screen_key=False))
    hint = lambda: main.EngineRoomApp._sensor_hint(app)
    assert hint() is False  # nothing sampled yet
    live.push({"t": 1.0, "system_load": 7.0, "system_in": 0.0, "battery_power": -7.0, "screen_power": None,
               "chip_power": 1.0, "has_battery": True, "has_screen_power": False})
    assert hint() is True  # a laptop with none of the screen keys the app knows
    app._reader.has_screen_key = True
    assert hint() is False
    app._thermals.guessing = ("gpu",)
    assert hint() is True
