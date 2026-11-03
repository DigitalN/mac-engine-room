"""Fan control: the curves, the safety override, the tuning limits, and the controller
against a stand-in for the helper (nothing here talks to the real one)."""

import pytest

import fans
from fans import (ENABLED, MACOS, NOT_REGISTERED, REQUIRES_APPROVAL, Curve, FanControl, HelperError, Safety,
                  manual_speeds, min_fraction, profile, speeds, trend, validate_tuning)
from settings import DEFAULTS, validate

M1_PRO = [(1499, 4296), (1499, 4744)]  # this M1 Pro's fans: (minimum, maximum) rpm
FLOOR = min_fraction(M1_PRO)


def run(curve, temps, start=0.0):
    """Ticks once a second; returns what each tick asked for."""
    return [curve.tick(start + i, t, FLOOR) for i, t in enumerate(temps)]


def test_balanced_waits_8_seconds_then_starts_at_the_fans_minimum():
    out = run(Curve(profile("balanced")), [56] * 9)
    assert out[:8] == [None] * 8  # 0 through 7 seconds at 55°+
    assert out[8] == FLOOR
    assert speeds(out[8], M1_PRO) == [1499, 1499]  # each fan at its own minimum


def test_a_spike_shorter_than_the_trigger_leaves_the_fans_with_macos():
    assert run(Curve(profile("balanced")), [56, 70, 70, 54, 56, 56, 56]) == [None] * 7


def test_speed_follows_the_curve_and_the_ramp():
    curve = Curve(profile("performance"))  # 55° to 65°, 85% top, up 10%/s
    assert run(curve, [60] * 6)[4:] == [FLOOR, pytest.approx(FLOOR + 0.10)]  # the minimum, then up 10% a second
    out = run(curve, [60, 65, 65, 65, 65, 65], start=6)
    assert out[0] == pytest.approx(0.425)  # halfway up a linear 85%
    assert out[-1] == pytest.approx(0.85)  # top speed, and no higher


def test_ease_in_is_quiet_low_down():
    curve = Curve(profile("balanced"))
    run(curve, [56] * 9)
    assert run(curve, [62.5] * 20, start=9)[-1] == FLOOR  # (0.5)² × 60% = 15%, under the fans' minimum


def test_fans_go_back_to_macos_only_after_5_seconds_at_or_below_fans_stop():
    curve = Curve(profile("balanced"))  # stops at 50°
    run(curve, [60] * 9)
    out = run(curve, [50, 50, 50, 50, 50, 50], start=9)
    assert out[:5] == [FLOOR] * 5 and out[5] is None
    assert not curve.holding


def test_a_brief_dip_below_fans_stop_keeps_the_fans():
    curve = Curve(profile("balanced"))
    run(curve, [60] * 9)
    assert None not in run(curve, [49, 49, 49, 52, 49, 49, 49, 49], start=9)


def test_max_jumps_to_full_speed_after_5_seconds_and_ramps_down():
    curve = Curve(profile("max"))
    out = run(curve, [66] * 6)
    assert out[:5] == [None] * 5 and out[5] == 1.0
    assert run(curve, [60], start=6) == [pytest.approx(0.975)]  # down 2.5%/s


def test_smart_speeds_up_sooner_while_the_temperature_rises():
    flat, rising = Curve(profile("smart")), Curve(profile("smart"))
    run(flat, [70] * 15)
    run(rising, [64 + 0.4 * i for i in range(15)])  # to 69.6°, still under flat's 70°
    assert rising.fraction > flat.fraction


def test_the_trend_is_a_fitted_line_so_one_noisy_reading_barely_moves_it():
    steady = [(t, 60.0) for t in range(10)]
    assert trend(steady) == 0
    assert abs(trend(steady[:-1] + [(9, 66.0)])) < 0.4  # newest minus oldest would say 0.67 °C/s


def test_after_sleep_the_curve_starts_over_from_the_temperatures_then():
    curve = Curve(profile("balanced"))
    run(curve, [70] * 20)
    curve.reset()
    assert run(curve, [70] * 8, start=100) == [None] * 8  # the trigger again, not the old speed


def test_tuning_is_clamped_to_the_limits_in_settings():
    assert validate_tuning({"balanced": {"start": 40, "full": 99, "top": 10}}) == {
        "balanced": {"start": 50.0, "full": 94.0, "top": 30}}
    assert validate_tuning({"performance": {"start": 80, "full": 81}}) == {"performance": {"start": 80.0, "full": 85.0}}
    assert validate_tuning({"balanced": {"start": 68}}) == {"balanced": {"start": 68.0, "full": 73.0}}
    assert validate_tuning({"max": {"start": 70, "full": 90, "top": 50}}) == {"max": {"start": 70.0}}
    assert validate_tuning({"manual": {"start": 60}, "smart": "hot", "balanced": {"start": True}}) == {}
    assert validate_tuning({"smart": {"start": 54.444}}) == {"smart": {"start": 54.4}}  # 130 °F, typed


def test_tuning_moves_fans_stop_with_fans_start():
    tuned = profile("balanced", {"balanced": {"start": 60, "full": 75, "top": 50}})
    assert (tuned.start, tuned.stop, tuned.full, tuned.top) == (60, 55, 75, 0.5)
    assert profile("smart").stop == 50 and profile("max").stop == 50
    assert profile(MACOS) is None and profile("manual") is None


def test_settings_start_on_macos_default_and_keep_valid_choices():
    assert DEFAULTS["fan_profile"] == MACOS
    s = validate({"fan_profile": "turbo", "fan_manual_level": 3, "fan_tuning": {"max": {"start": 60}}})
    assert (s["fan_profile"], s["fan_manual_level"], s["fan_tuning"]) == (MACOS, 1.0, {"max": {"start": 60.0}})
    assert validate({"fan_profile": "manual"})["fan_profile"] == "manual"


def test_the_menu_shows_three_different_profiles():
    assert DEFAULTS["menu_fan_profiles"] == ["macos", "balanced", "max"]
    assert validate({"menu_fan_profiles": ["manual", "smart", "macos"]})["menu_fan_profiles"] == ["manual", "smart", "macos"]
    for bad in (["macos", "balanced"], ["macos", "macos", "max"], ["macos", "balanced", "turbo"], "max", None):
        assert validate({"menu_fan_profiles": bad})["menu_fan_profiles"] == ["macos", "balanced", "max"]


def test_manual_spans_each_fans_own_range():
    assert manual_speeds(0, M1_PRO) == [1499, 1499]
    assert manual_speeds(1, M1_PRO) == [4296, 4744]
    assert manual_speeds(0.5, M1_PRO) == [2898, 3122]


def test_safety_follows_macos_thermal_pressure():
    safety = Safety()
    assert safety.tick(0, {"pressure": 1}) is None
    assert safety.tick(29, {"pressure": 1}) is None
    assert safety.tick(30, {"pressure": 1}) == "pressure"  # Fair for 30 seconds
    assert safety.tick(31, {"pressure": 0}) is None
    assert Safety().tick(0, {"pressure": 2}) == "pressure"  # Serious at once


def test_safety_waits_its_time_for_each_sensor_and_clears_5_degrees_lower():
    safety = Safety()
    assert safety.tick(0, {"hotspot": 118}) is None
    assert safety.tick(10, {"hotspot": 119}) == "hotspot"
    assert safety.tick(11, {"hotspot": 115}) == "hotspot"  # not 5° under yet
    assert safety.tick(12, {"hotspot": 112}) is None
    for limit, after, key in ((95, 30, "chassis"), (50, 60, "battery")):
        safety = Safety()
        assert safety.tick(0, {key: limit}) is None and safety.tick(after - 1, {key: limit}) is None
        assert safety.tick(after, {key: limit}) == key


def test_one_bad_hotspot_sensor_cant_trip_the_override():
    import thermals

    keys = {"hotspots": ("TCDX", "TCMb", "TCMz"), "chassis": ("TS0P",)}
    sample = thermals.normalize({"TCDX": 128.0, "TCMb": 63.5, "TCMz": 72.0, "TS0P": 44.4}, keys, now=0)
    assert (sample["hotspot"], sample["chassis"]) == (72.0, 44.4)


class FakeHelper:
    unavailable_reason = None

    def __init__(self, status=ENABLED):
        self.service = status
        self.sent = []
        self.replies = {}
        self.registered = 0
        self.closed = 0

    def status(self):
        return self.service

    def register(self):
        self.registered += 1
        self.service = REQUIRES_APPROVAL

    def request(self, line, timeout=5.0):
        self.sent.append(line)
        reply = self.replies.get(line.split()[0], "")
        if isinstance(reply, HelperError):
            raise reply
        return reply

    def close(self):
        self.closed += 1

    def open_login_items(self):
        pass


class FakeSettings:
    def __init__(self, **values):
        self.values = {**DEFAULTS, **values}

    def get(self):
        return dict(self.values)


@pytest.fixture
def control(monkeypatch):
    monkeypatch.setattr(fans, "thermal_pressure", lambda: 0)

    def make(profile_id="balanced", status=ENABLED, limits=M1_PRO, **settings):
        helper = FakeHelper(status)
        c = FanControl(FakeSettings(fan_profile=profile_id, **settings), helper, lambda: limits)
        return c, helper

    return make


def feed(c, temps, start=0.0):
    for i, t in enumerate(temps):
        c.feed({"cpu": t, "gpu": t - 5}, {"temperature": 30.0})
        c._tick(start + i)


def test_macos_default_never_touches_the_helper(control):
    c, helper = control(MACOS, status=NOT_REGISTERED)
    feed(c, [90] * 20)
    assert (c.status["state"], helper.registered, helper.sent) == ("off", 0, [])
    assert not c.running


@pytest.mark.parametrize("never_registered", [NOT_REGISTERED, fans.NOT_FOUND])  # macOS says NOT_FOUND
def test_the_helper_is_registered_the_first_time_a_profile_is_chosen_and_waits_for_approval(control, never_registered):
    c, helper = control("balanced", status=never_registered)
    feed(c, [90] * 20)
    assert (helper.registered, c.status["state"], helper.sent) == (1, "approval", [])
    helper.service = ENABLED  # the user allowed it in Login Items
    feed(c, [90] * 20, start=20)
    assert c.status["state"] == "holding" and helper.sent[0].startswith("set ")


def test_holding_sets_speeds_only_when_they_change_and_checks_in_otherwise(control):
    c, helper = control("manual", fan_manual_level=0.5)
    feed(c, [60] * 3)
    assert helper.sent == ["set 2898 3122", "checkin", "checkin"]
    assert c.status == {"profile": "manual", "state": "holding", "targets": [2898, 3122]}
    assert c.running and c.holding


def test_fans_go_back_to_macos_and_the_connection_closes_when_the_curve_lets_go(control):
    c, helper = control("balanced")
    feed(c, [60] * 9)
    assert c.holding
    feed(c, [45] * 6, start=9)  # 5 seconds at or below Fans stop
    assert helper.sent[-1] == "auto" and not c.holding and helper.closed
    assert c.status == {"profile": "balanced", "state": "watching", "start": 55}


def test_switching_profiles_carries_on_from_the_current_speed(control):
    c, helper = control("manual", fan_manual_level=1.0)
    feed(c, [60])
    c._settings.values["fan_profile"] = "balanced"
    feed(c, [60], start=1)
    assert c.holding and "auto" not in helper.sent  # ramps down from full speed instead
    c._settings.values["fan_profile"] = MACOS
    feed(c, [60], start=2)
    assert helper.sent[-1] == "auto" and c.status["state"] == "off"


def test_the_safety_override_forces_full_speed_only_while_holding(control, monkeypatch):
    c, helper = control("balanced")
    monkeypatch.setattr(fans, "thermal_pressure", lambda: 3)
    feed(c, [45] * 3)
    assert helper.sent == []  # with macOS: nothing to override
    feed(c, [60] * 9, start=3)
    assert helper.sent[-1] == "set 4296 4744"
    assert (c.status["state"], c.status["reason"], c.status["pressure"]) == ("override", "pressure", 3)
    assert Safety.describe("pressure", 3) == "macOS reports critical thermal pressure"


def test_another_app_holding_the_fans_is_left_alone(control):
    c, helper = control("manual")
    helper.replies["set"] = HelperError("other-app")
    feed(c, [60] * 3)
    assert helper.sent == ["set 1499 1499"]  # asked once; no retry for 10 seconds
    assert c.status["state"] == "other" and not c.holding


def test_after_sleep_or_a_missed_check_in_the_fans_are_taken_again_from_scratch(control):
    c, helper = control("manual")
    feed(c, [60])
    helper.replies["checkin"] = "released"
    feed(c, [60], start=1)
    assert not c.holding
    helper.replies.pop("checkin")
    feed(c, [60], start=2)
    assert helper.sent[-1] == "set 1499 1499" and c.holding
    c.did_wake()
    feed(c, [60], start=3)
    assert helper.sent[-1] == "set 1499 1499"  # set again, not just a check-in


def test_quitting_hands_the_fans_back(control):
    c, helper = control("manual")
    feed(c, [60])
    c.stop()
    assert helper.sent[-1] == "auto" and not c.holding


def test_a_mac_without_fans_has_no_fan_control(control):
    c, helper = control("balanced", limits=[])
    feed(c, [90] * 10)
    assert c.status["state"] == "nofans" and helper.sent == [] and helper.registered == 0


def test_macos_is_asked_about_the_helper_once_it_is_allowed_only_when_it_cant_be_reached(control):
    c, helper = control("manual")
    asked = []
    helper.status = lambda: asked.append(1) or helper.service
    feed(c, [60] * 10)
    assert len(asked) == 1  # backgroundtaskmanagementd can take seconds to answer
    helper.replies["checkin"] = HelperError("unreachable")
    feed(c, [60], start=10)
    assert c.status["state"] == "error" and not c.holding
    helper.replies.pop("checkin")
    feed(c, [60], start=41)  # after the 30-second retry
    assert len(asked) == 2 and c.holding
