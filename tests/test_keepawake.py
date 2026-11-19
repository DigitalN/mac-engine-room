"""Keep Awake's rules (the pure Model), its settings, its sentence, and the controller with a
stand-in for IOKit. Times are local: each test runs in a fixed time zone."""

import json
import os
import time
from datetime import datetime

import pytest

import keepawake
from keepawake import (HOUR, OFF, SCHEDULE, SLEEP, TIMER, TWO_HOURS, UNTIL_SLEEP, Env, KeepAwake, Model,
                       Status, block_end, first_weekday, sentence, validate_battery,
                       validate_hours, week)


@pytest.fixture(autouse=True)
def local_time(monkeypatch):
    """New York: it has daylight saving time, so its edges can be tested."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(text):
    """'2026-11-19 16:30' (a Thursday), local time, as seconds since the epoch."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


HOURS = {**keepawake.DEFAULT_HOURS, "on": True}  # Mon–Fri 9:00–17:00
NO_HOURS = keepawake.DEFAULT_HOURS
NO_RULE = keepawake.DEFAULT_BATTERY
RULE = {"on": True, "level": 20}
AWAKE = Env()


def run(model, when, hours=HOURS, rule=NO_RULE, env=AWAKE):
    return model.evaluate(at(when) if isinstance(when, str) else when, hours, rule, env)


def status(model, when, **kw):
    return run(model, when, **kw)[0]


# ── settings ─────────────────────────────────────────────

def test_work_hours_are_off_until_turned_on_with_monday_to_friday_nine_to_five():
    assert validate_hours(None) == {"on": False, "start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4]}


def test_work_hours_end_after_they_start():
    good = validate_hours({"on": True, "start": "08:30", "end": "12:15", "days": [5, 1, 1]})
    assert good == {"on": True, "start": "08:30", "end": "12:15", "days": [1, 5]}
    for start, end in (("17:00", "09:00"), ("09:00", "09:00"), ("22:00", "02:00")):  # no blocks past midnight
        assert validate_hours({"start": start, "end": end})[("start")] == "09:00"
        assert validate_hours({"start": start, "end": end})["end"] == "17:00"


def test_work_hours_junk_falls_back_to_defaults():
    for junk in ({"start": "9:00"}, {"start": "24:00"}, {"end": "17:60"}, {"start": 540}, {"on": "yes"},
                 {"days": []}, {"days": [7]}, {"days": [-1]}, {"days": "mon"}, {"days": [True]}, {"days": [1.0]}):
        s = validate_hours(junk)
        assert s == keepawake.DEFAULT_HOURS, junk


def test_battery_rule_is_off_at_20_percent_and_only_offers_its_levels():
    assert validate_battery(None) == {"on": False, "level": 20}
    assert validate_battery({"on": True, "level": 40}) == {"on": True, "level": 40}
    for level in (0, 15, 60, "20", True, None):
        assert validate_battery({"level": level})["level"] == 20


def test_settings_validates_keep_awake_like_every_other_value(tmp_path):
    from settings import DEFAULTS, Settings, validate

    assert validate({})["keep_awake_hours"] == DEFAULTS["keep_awake_hours"]
    store = Settings(tmp_path / "settings.json")
    store.update({"keep_awake_hours": {"on": True, "start": "10:00", "end": "09:00", "days": [0]}})
    assert store.get()["keep_awake_hours"] == {"on": True, "start": "09:00", "end": "17:00", "days": [0]}
    store.update({"keep_awake_battery": {"on": True, "level": 30}})
    assert json.loads((tmp_path / "settings.json").read_text())["keep_awake_battery"] == {"on": True, "level": 30}


def test_the_week_starts_on_the_macs_first_weekday():
    assert first_weekday(2) == 0  # NSCalendar's Monday
    assert first_weekday(1) == 6  # Sunday, as in the US
    assert first_weekday(7) == 5  # Saturday
    assert first_weekday(0) == 0 and first_weekday(None) == 0
    assert week(6) == [6, 0, 1, 2, 3, 4, 5]
    assert week(0) == [0, 1, 2, 3, 4, 5, 6]


# ── the block ────────────────────────────────────────────

def test_the_block_is_local_time_on_chosen_days():
    assert block_end(HOURS, at("2026-11-19 09:00")) == at("2026-11-19 17:00")  # Thursday
    assert block_end(HOURS, at("2026-11-19 16:59")) == at("2026-11-19 17:00")
    assert block_end(HOURS, at("2026-11-19 17:00")) is None
    assert block_end(HOURS, at("2026-11-19 08:59")) is None
    assert block_end(HOURS, at("2026-11-21 12:00")) is None  # Saturday
    assert block_end(NO_HOURS, at("2026-11-19 12:00")) is None


def test_the_block_follows_daylight_saving_time():
    # Clocks went forward at 2:00 on 2026-03-08 (a Sunday) and back at 2:00 on 2026-11-01.
    early = {**HOURS, "start": "01:00", "end": "04:00", "days": [6]}
    assert block_end(early, at("2026-03-08 03:30")) == at("2026-03-08 04:00")
    assert at("2026-03-08 04:00") - at("2026-03-08 01:00") == 2 * 3600  # an hour shorter that day
    assert block_end(early, at("2026-11-01 03:30")) == at("2026-11-01 04:00")
    assert at("2026-11-01 04:00") - at("2026-11-01 01:00") == 4 * 3600  # an hour longer
    # The Monday after: nine to five on the new clock.
    assert block_end(HOURS, at("2026-03-09 09:00")) == at("2026-03-09 17:00")


def test_the_block_follows_a_change_of_time_zone(monkeypatch):
    noon_here = at("2026-11-19 12:00")
    assert block_end(HOURS, noon_here) is not None
    monkeypatch.setenv("TZ", "Asia/Tokyo")  # 02:00 on Friday there
    time.tzset()
    assert block_end(HOURS, noon_here) is None


# ── picks ────────────────────────────────────────────────

def test_one_hour_holds_for_an_hour_then_lets_go():
    m = Model()
    m.choose(HOUR, at("2026-11-21 10:00"), HOURS)  # a Saturday: no work hours
    s, notes, _ = run(m, "2026-11-21 10:00")
    assert (s.holding, s.kind, s.until, s.lit) == (True, TIMER, at("2026-11-21 11:00"), HOUR)
    assert notes == ["Keep Awake on for 1 hour (until 11:00)"]
    assert status(m, "2026-11-21 10:59").holding
    s, notes, declare = run(m, "2026-11-21 11:00")
    assert (s.holding, s.lit) == (False, OFF)
    assert notes == ["Keep Awake off (1 hour is up)"]
    assert declare  # the Mac is in use: the screen gets its full display-sleep time from here


def test_the_same_pick_again_restarts_it_and_the_last_pick_wins_even_when_shorter():
    m = Model()
    m.choose(TWO_HOURS, at("2026-11-21 10:00"), HOURS)
    assert status(m, "2026-11-21 10:00").until == at("2026-11-21 12:00")
    m.choose(TWO_HOURS, at("2026-11-21 10:30"), HOURS)
    s, notes, _ = run(m, "2026-11-21 10:30")
    assert s.until == at("2026-11-21 12:30") and notes == ["Keep Awake on for 2 hours (until 12:30)"]
    m.choose(HOUR, at("2026-11-21 10:45"), HOURS)
    assert status(m, "2026-11-21 10:45").until == at("2026-11-21 11:45")
    assert not status(m, "2026-11-21 11:50").holding
    m.choose(TWO_HOURS, at("2026-11-21 12:00"), HOURS)
    assert run(m, "2026-11-21 12:00")[1] == ["Keep Awake on for 2 hours (until 14:00)"]
    assert run(m, "2026-11-21 14:00")[1] == ["Keep Awake off (2 hours are up)"]


def test_until_sleep_and_a_timer_replace_each_other():
    m = Model()
    m.choose(HOUR, at("2026-11-21 10:00"), HOURS)
    m.choose(UNTIL_SLEEP, at("2026-11-21 10:10"), HOURS)
    s, notes, _ = run(m, "2026-11-21 12:00")  # long past the hour
    assert (s.kind, s.lit, s.holding) == (SLEEP, UNTIL_SLEEP, True)
    assert notes == ["Keep Awake on until the Mac sleeps"]
    m.choose(HOUR, at("2026-11-21 12:00"), HOURS)
    assert status(m, "2026-11-21 12:00").kind == TIMER
    m.slept()  # Until Sleep is gone: sleep doesn't end a timer
    assert status(m, "2026-11-21 12:30").holding


def test_timers_run_on_the_wall_clock_through_sleep():
    m = Model()
    m.choose(TWO_HOURS, at("2026-11-21 10:00"), HOURS)
    run(m, "2026-11-21 10:00")
    m.slept()
    assert status(m, "2026-11-21 11:30").holding  # woke before the end: still on, until 12:00
    assert status(m, "2026-11-21 11:30").until == at("2026-11-21 12:00")
    m.slept()
    s, notes, declare = run(m, "2026-11-21 13:00", env=Env(screens_awake=False))  # woke after it ended
    assert not s.holding and notes == ["Keep Awake off (2 hours are up)"]


def test_until_sleep_ends_at_the_next_sleep_and_doesnt_declare_activity():
    m = Model()
    m.choose(UNTIL_SLEEP, at("2026-11-21 10:00"), HOURS)
    run(m, "2026-11-21 10:00")
    assert status(m, "2026-11-22 03:00").holding  # however long it is
    m.slept()
    s, notes, declare = run(m, "2026-11-22 03:00")
    assert not s.holding and s.lit == OFF
    assert notes == ["Keep Awake off (the Mac is going to sleep)"]
    assert not declare  # never ask for the screen as the Mac goes to sleep
    m.slept()
    assert run(m, "2026-11-22 03:01")[1] == []  # nothing more to say


def test_unknown_choice_is_ignored():
    m = Model()
    m.choose("forever", at("2026-11-21 10:00"), HOURS)
    assert not status(m, "2026-11-21 10:00").holding


# ── work hours ───────────────────────────────────────────

def test_work_hours_hold_inside_the_block_with_no_tile_lit():
    m = Model()
    s, notes, _ = run(m, "2026-11-19 08:59")
    assert not s.holding and notes == []
    s, notes, _ = run(m, "2026-11-19 09:00")
    assert (s.holding, s.kind, s.until, s.lit) == (True, SCHEDULE, at("2026-11-19 17:00"), None)
    assert notes == ["Keep Awake on until 17:00 (work hours)"]
    assert run(m, "2026-11-19 12:00")[1] == []
    s, notes, declare = run(m, "2026-11-19 17:00")
    assert not s.holding and notes == ["Keep Awake off (work hours are over)"] and declare


def test_work_hours_take_effect_when_turned_on_inside_the_block_and_off_when_turned_off():
    m = Model()
    assert not status(m, "2026-11-19 10:00", hours=NO_HOURS).holding
    assert status(m, "2026-11-19 10:01").holding
    s, notes, _ = run(m, "2026-11-19 10:02", hours=NO_HOURS)
    assert not s.holding and notes == ["Keep Awake off (work hours turned off)"]


def test_changing_the_hours_moves_the_end():
    m = Model()
    run(m, "2026-11-19 10:00")
    s, notes, _ = run(m, "2026-11-19 10:01", hours={**HOURS, "end": "18:30"})
    assert s.until == at("2026-11-19 18:30") and notes == ["Keep Awake on until 18:30 (work hours)"]
    s, notes, _ = run(m, "2026-11-19 10:02", hours={**HOURS, "end": "10:00", "start": "08:00"})
    assert notes == ["Keep Awake off (work hours changed)"]


def test_work_hours_never_wake_the_mac_they_hold_once_it_is_awake():
    """Nothing is scheduled: asleep all morning, the Mac holds the screen on from when it's opened."""
    m = Model()
    run(m, "2026-11-19 08:00", env=Env(screens_awake=False))
    s, notes, _ = run(m, "2026-11-19 09:00", env=Env(screens_awake=False))  # a dark wake, say
    assert (s.kind, s.paused, s.holding) == (SCHEDULE, "screens", False)
    s, notes, _ = run(m, "2026-11-19 11:00")  # the lid opened
    assert s.holding and notes == ["Keep Awake resumed"]


def test_one_hour_during_work_hours_ends_after_the_hour_and_the_day_stays_off():
    m = Model()
    run(m, "2026-11-19 09:30")
    m.choose(HOUR, at("2026-11-19 10:00"), HOURS)
    s, notes, _ = run(m, "2026-11-19 10:00")
    assert (s.kind, s.until, s.lit) == (TIMER, at("2026-11-19 11:00"), HOUR)  # its own terms, not until 17:00
    assert notes == ["Keep Awake on for 1 hour (until 11:00)"]
    assert sentence(s, at("2026-11-19 10:01")) == "Keep Awake ends at 11:00 (59 minutes)"
    s, notes, declare = run(m, "2026-11-19 11:00")
    assert (s.holding, s.lit, s.off_today) == (False, OFF, True) and declare
    assert notes == ["Keep Awake off (1 hour is up, for the rest of today's work hours)"]
    assert sentence(s, 0) is None
    assert not status(m, "2026-11-19 15:00").holding
    s, notes, _ = run(m, "2026-11-20 09:00")  # back by itself the next workday
    assert s.kind == SCHEDULE and notes == ["Keep Awake on until 17:00 (work hours)"]


def test_two_hours_late_in_work_hours_runs_past_their_end():
    m = Model()
    m.choose(TWO_HOURS, at("2026-11-19 16:30"), HOURS)
    s, notes, _ = run(m, "2026-11-19 16:30")
    assert (s.kind, s.until, s.lit) == (TIMER, at("2026-11-19 18:30"), TWO_HOURS)
    assert run(m, "2026-11-19 17:00")[0].holding
    s, notes, _ = run(m, "2026-11-19 18:30")
    assert not s.holding and notes == ["Keep Awake off (2 hours are up)"]  # work hours were over by then


def test_until_sleep_during_work_hours_then_a_wake_the_same_day_stays_off():
    m = Model()
    run(m, "2026-11-19 10:00")
    m.choose(UNTIL_SLEEP, at("2026-11-19 10:30"), HOURS)
    assert status(m, "2026-11-19 12:00").kind == SLEEP
    m.slept()
    s, notes, declare = run(m, "2026-11-19 12:01", env=Env(screens_awake=False))
    assert notes == ["Keep Awake off (the Mac is going to sleep, for the rest of today's work hours)"] and not declare
    s, notes, _ = run(m, "2026-11-19 13:30")
    assert (s.holding, s.lit, s.off_today, notes) == (False, OFF, True, [])
    assert status(m, "2026-11-20 09:30").kind == SCHEDULE


def test_until_sleep_carries_past_the_end_of_the_block():
    m = Model()
    m.choose(UNTIL_SLEEP, at("2026-11-19 16:00"), HOURS)
    run(m, "2026-11-19 16:00")
    s, notes, _ = run(m, "2026-11-19 20:00")
    assert s.holding and s.kind == SLEEP and notes == []


def test_off_inside_the_block_is_off_for_the_rest_of_today_across_sleeps():
    m = Model()
    run(m, "2026-11-19 10:00")
    m.choose(OFF, at("2026-11-19 10:30"), HOURS)
    s, notes, declare = run(m, "2026-11-19 10:30")
    assert (s.holding, s.lit, s.off_today) == (False, OFF, True)
    assert notes == ["Keep Awake off (Off chosen, for the rest of today's work hours)"]
    m.slept()  # lunch
    s, notes, _ = run(m, "2026-11-19 13:30")
    assert not s.holding and s.off_today and notes == []
    assert not status(m, "2026-11-19 17:00").off_today  # the block's over: nothing left to say
    s, notes, _ = run(m, "2026-11-20 09:00")  # back by itself the next workday
    assert s.holding and notes == ["Keep Awake on until 17:00 (work hours)"]


def test_off_for_today_comes_back_after_a_weekend():
    m = Model()
    m.choose(OFF, at("2026-11-20 16:00"), HOURS)  # Friday
    assert not status(m, "2026-11-20 16:00").holding
    assert status(m, "2026-11-23 09:00").holding  # Monday, the Mac asleep all weekend


def test_a_pick_after_off_for_today_holds_on_its_own_terms_then_off_is_back():
    m = Model()
    run(m, "2026-11-19 09:00")
    m.choose(OFF, at("2026-11-19 09:30"), HOURS)
    run(m, "2026-11-19 09:30")
    m.choose(HOUR, at("2026-11-19 10:00"), HOURS)
    s, notes, _ = run(m, "2026-11-19 10:00")
    assert (s.holding, s.kind, s.until, s.lit) == (True, TIMER, at("2026-11-19 11:00"), HOUR)  # not until 17:00
    assert notes == ["Keep Awake on for 1 hour (until 11:00)"]
    assert sentence(s, at("2026-11-19 10:00")) == "Keep Awake ends at 11:00 (1 hour)"
    s, notes, declare = run(m, "2026-11-19 11:05")
    assert (s.holding, s.lit, s.off_today) == (False, OFF, True)
    assert notes == ["Keep Awake off (1 hour is up, for the rest of today's work hours)"] and declare
    assert sentence(s, 0) is None
    assert status(m, "2026-11-20 09:00").kind == SCHEDULE  # back the next workday


def test_until_sleep_after_off_for_today_ends_at_sleep_and_off_is_back_at_wake():
    m = Model()
    m.choose(OFF, at("2026-11-19 10:00"), HOURS)
    run(m, "2026-11-19 10:00")
    m.choose(UNTIL_SLEEP, at("2026-11-19 11:00"), HOURS)
    assert status(m, "2026-11-19 11:00").kind == SLEEP
    m.slept()
    s, notes, declare = run(m, "2026-11-19 12:00", env=Env(screens_awake=False))
    assert not s.holding and s.off_today and not declare
    assert notes == ["Keep Awake off (the Mac is going to sleep, for the rest of today's work hours)"]
    s, notes, _ = run(m, "2026-11-19 13:30")  # woke the same day
    assert (s.holding, s.lit, notes) == (False, OFF, [])
    assert sentence(s, 0) is None


def test_sleeping_at_lunch_doesnt_end_work_hours():
    m = Model()
    run(m, "2026-11-19 11:00")
    m.slept()
    s, notes, _ = run(m, "2026-11-19 11:01", env=Env(screens_awake=False))
    assert s.kind == SCHEDULE and not s.holding and notes == ["Keep Awake paused (the screens are asleep)"]
    s, notes, _ = run(m, "2026-11-19 13:00")
    assert s.holding and notes == ["Keep Awake resumed"]


def test_work_hours_across_daylight_saving_and_midnight():
    m = Model()
    early = {**HOURS, "start": "00:00", "end": "23:59", "days": [6]}  # all of a Sunday
    assert status(m, "2026-03-08 03:00", hours=early).until == at("2026-03-08 23:59")
    assert not status(m, "2026-03-09 00:00", hours=early).holding  # ends before midnight


# ── pauses ───────────────────────────────────────────────

def test_locking_pauses_and_unlocking_resumes_if_time_remains():
    m = Model()
    m.choose(HOUR, at("2026-11-21 10:00"), HOURS)
    run(m, "2026-11-21 10:00")
    s, notes, declare = run(m, "2026-11-21 10:10", env=Env(locked=True))
    assert (s.holding, s.paused, s.lit) == (False, "locked", HOUR)
    assert notes == ["Keep Awake paused (the screen is locked)"] and not declare  # nobody there
    s, notes, _ = run(m, "2026-11-21 10:20")
    assert s.holding and notes == ["Keep Awake resumed"]
    run(m, "2026-11-21 10:30", env=Env(locked=True))
    s, notes, _ = run(m, "2026-11-21 11:30")  # unlocked after the hour
    assert not s.holding and notes == ["Keep Awake off (1 hour is up)"]


def test_switching_to_another_users_session_counts_as_locking():
    m = Model()
    m.choose(UNTIL_SLEEP, at("2026-11-21 10:00"), HOURS)
    run(m, "2026-11-21 10:00")
    s, notes, _ = run(m, "2026-11-21 10:10", env=Env(away=True))
    assert not s.holding and notes == ["Keep Awake paused (the screen is locked)"]


def test_battery_rule_pauses_only_on_battery_below_its_level():
    m = Model()
    m.choose(TWO_HOURS, at("2026-11-21 10:00"), HOURS)
    run(m, "2026-11-21 10:00", env=Env(has_battery=True, on_adapter=False, level=25), rule=RULE)
    s, notes, declare = run(m, "2026-11-21 10:10", env=Env(has_battery=True, on_adapter=False, level=19), rule=RULE)
    assert (s.holding, s.paused, s.battery_low, s.lit) == (False, "battery", True, TWO_HOURS)
    assert notes == ["Keep Awake paused (the battery is below 20%)"] and declare
    assert sentence(s, 0) == "Paused because the battery is below 20%"
    # 20% isn't below 20%.
    assert status(m, "2026-11-21 10:11", env=Env(has_battery=True, on_adapter=False, level=20), rule=RULE).holding
    run(m, "2026-11-21 10:12", env=Env(has_battery=True, on_adapter=False, level=19), rule=RULE)
    # Plugging in brings it back: on the adapter it holds even below the level.
    s, notes, _ = run(m, "2026-11-21 10:20", env=Env(has_battery=True, on_adapter=True, level=12), rule=RULE)
    assert s.holding and notes == ["Keep Awake resumed"]
    # The timer kept counting while paused.
    run(m, "2026-11-21 10:30", env=Env(has_battery=True, on_adapter=False, level=12), rule=RULE)
    s, notes, _ = run(m, "2026-11-21 12:10", env=Env(has_battery=True, on_adapter=True, level=15), rule=RULE)
    assert not s.holding and notes == ["Keep Awake off (2 hours are up)"]


def test_battery_rule_applies_to_work_hours_too_and_not_when_off_or_on_a_desktop():
    m = Model()
    low = Env(has_battery=True, on_adapter=False, level=5)
    s = status(m, "2026-11-19 10:00", env=low, rule=RULE)
    assert s.kind == SCHEDULE and s.paused == "battery" and s.lit is None
    assert status(Model(), "2026-11-19 10:00", env=low, rule=NO_RULE).holding
    desktop = Env(has_battery=False, on_adapter=True, level=None)
    assert status(Model(), "2026-11-19 10:00", env=desktop, rule=RULE).holding


def test_battery_rule_says_nothing_when_nothing_would_hold_the_screen_on():
    m = Model()
    s = status(m, "2026-11-21 10:00", env=Env(has_battery=True, on_adapter=False, level=5), rule=RULE)
    assert not s.battery_low and sentence(s, 0) is None


# ── the sentence ─────────────────────────────────────────

def test_the_line_for_each_state():
    now = at("2026-11-19 12:22")
    short = lambda t: keepawake.short_clock(t, TWELVE, False, end=True)
    assert sentence(Status(TIMER, at("2026-11-19 13:22"), HOUR), now + 60, short) == (
        "Keep Awake ends at 1:22 PM (59 minutes)")
    assert sentence(Status(TIMER, at("2026-11-19 15:22"), TWO_HOURS), at("2026-11-19 13:23"), short) == (
        "Keep Awake ends at 3:22 PM (1 hour 59 minutes)")
    assert sentence(Status(TIMER, at("2026-11-19 13:00") + 30, HOUR), now, short) == "Keep Awake ends at 1 PM (39 minutes)"
    assert sentence(Status(SCHEDULE, at("2026-11-19 17:00")), now, short) == "Keep Awake ends at 5 PM (4 hours 38 minutes)"
    assert sentence(Status(SCHEDULE, at("2026-11-19 12:00")), now - 3600, short) == "Keep Awake ends at Noon (38 minutes)"
    assert sentence(Status(SCHEDULE, at("2026-11-20 00:00")), at("2026-11-19 22:00"), short) == (
        "Keep Awake ends at Midnight (2 hours)")
    assert sentence(Status(SLEEP, None, UNTIL_SLEEP), now, short) == "Keep Awake ends when the Mac sleeps"
    assert sentence(Status(TIMER, now + 60, HOUR, "battery", battery_low=True, level=30), now, short) == (
        "Paused because the battery is below 30%")
    # Nothing holding the screen on, Off for the rest of today's work hours included: no line.
    assert sentence(Status(), now) is None and sentence(Status(off_today=True), now) is None
    # Locked, or the screens asleep: the usual line (nobody can see the menu then anyway).
    assert sentence(Status(SLEEP, None, UNTIL_SLEEP, "locked"), now) == "Keep Awake ends when the Mac sleeps"
    # No full stop on any of them.
    assert not any(line.endswith(".") for line in (
        sentence(Status(TIMER, now + 90, HOUR), now, short), sentence(Status(SLEEP, None, UNTIL_SLEEP), now),
        sentence(Status(TIMER, now, HOUR, "battery", battery_low=True, level=20), now)))


def test_the_time_left_rounds_up_and_leaves_out_zero_parts():
    left = keepawake.time_left
    assert left(2 * 3600) == "2 hours"
    assert left(3600) == "1 hour"
    assert left(45 * 60) == "45 minutes"
    assert left(61 * 60) == "1 hour 1 minute"
    assert left(119 * 60) == "1 hour 59 minutes"
    assert left(3 * 3600 + 2 * 60) == "3 hours 2 minutes"
    assert left(60) == "1 minute"
    assert left(44 * 60 + 1) == "45 minutes"  # rounded up
    assert left(59 * 60 + 30) == "1 hour"
    assert left(20) == "1 minute" and left(0) == "1 minute" and left(-5) == "1 minute"  # never 0


def test_short_times_drop_00_on_whole_hours_of_a_12_hour_mac_only():
    short = keepawake.short_clock
    assert short(at("2026-11-19 13:00"), TWELVE, False) == "1 PM"
    assert short(at("2026-11-19 16:30"), TWELVE, False) == "4:30 PM"
    assert short(at("2026-11-19 13:07"), TWELVE, False) == "1:07 PM"
    assert short(at("2026-11-19 12:00"), TWELVE, False) == "Noon"
    assert short(at("2026-11-19 00:00"), TWELVE, False) == "Midnight"
    assert short(at("2026-11-19 17:00"), TWENTY_FOUR, True) == "17:00"
    assert short(at("2026-11-19 13:07"), TWENTY_FOUR, True) == "13:07"
    assert short(at("2026-11-20 00:00"), TWENTY_FOUR, True, end=True) == "24:00"


# ── the updater's relaunch ───────────────────────────────

def test_a_relaunch_brings_back_the_pick_with_the_same_end():
    old = Model()
    old.choose(TWO_HOURS, at("2026-11-21 13:00"), HOURS)
    run(old, "2026-11-21 13:00")
    data = json.loads(json.dumps(old.hand_over(at("2026-11-21 14:00"))))
    new = Model()
    assert new.take_over(data, at("2026-11-21 14:00") + 5)
    s, notes, _ = run(new, at("2026-11-21 14:00") + 5)
    assert (s.pick, s.kind, s.until) == (TWO_HOURS, TIMER, at("2026-11-21 15:00"))
    assert notes == ["Keep Awake on for 2 hours (until 15:00)"]


def test_a_relaunch_brings_back_until_sleep_and_off_for_today():
    old = Model()
    old.choose(UNTIL_SLEEP, at("2026-11-21 13:00"), HOURS)
    new = Model()
    assert new.take_over(old.hand_over(at("2026-11-21 13:05")), at("2026-11-21 13:05") + 30)
    assert status(new, "2026-11-21 13:06").kind == SLEEP

    old = Model()
    old.choose(OFF, at("2026-11-19 13:00"), HOURS)
    new = Model()
    assert new.take_over(old.hand_over(at("2026-11-19 13:05")), at("2026-11-19 13:05") + 30)
    s = status(new, "2026-11-19 13:06")
    assert not s.holding and s.off_today


def test_a_stale_or_broken_handover_brings_back_nothing():
    old = Model()
    old.choose(UNTIL_SLEEP, at("2026-11-21 13:00"), HOURS)
    data = old.hand_over(at("2026-11-21 13:00"))
    assert Model().take_over(data, at("2026-11-21 13:09"))  # a slow first launch of the new copy still counts
    for when in (at("2026-11-21 13:00") + keepawake.HANDOVER_FOR + 1, at("2026-11-21 12:59")):
        new = Model()
        assert not new.take_over(data, when)
        assert not status(new, "2026-11-21 13:10").holding
    for junk in (None, [], {}, {"written": "now"}, {"written": True}):
        assert not Model().take_over(junk, at("2026-11-21 13:00"))


def test_nothing_to_hand_over_without_a_pick_or_off_for_today():
    m = Model()
    run(m, "2026-11-19 10:00")  # work hours alone come back by themselves
    assert m.hand_over(at("2026-11-19 10:00")) is None
    assert not Model().take_over({"written": at("2026-11-19 10:00"), "caffeinate": 123}, at("2026-11-19 10:00"))


def test_handover_fields():
    m = Model()
    m.choose(OFF, at("2026-11-19 10:00"), HOURS)
    m.choose(HOUR, at("2026-11-19 10:30"), HOURS)
    assert m.hand_over(at("2026-11-19 10:40")) == {
        "written": at("2026-11-19 10:40"), "pick": HOUR, "until": at("2026-11-19 11:30"), "off_day": "2026-11-19"}


def test_caffeinate_bridges_the_relaunch_until_whatever_holds_the_screen_would_end():
    now = at("2026-11-19 16:58")
    assert keepawake.bridge_seconds(Status(TIMER, now + 125, HOUR), now) == 125
    assert keepawake.bridge_seconds(Status(TIMER, now + 0.2, HOUR), now) == 1
    assert keepawake.bridge_seconds(Status(TIMER, now + 7200, TWO_HOURS), now) == 600
    assert keepawake.bridge_seconds(Status(SCHEDULE, at("2026-11-19 17:00")), now) == 120
    assert keepawake.bridge_seconds(Status(SLEEP, None, UNTIL_SLEEP), now) == 600
    assert keepawake.bridge_seconds(Status(), now) is None  # nothing holds the screen on
    assert keepawake.bridge_seconds(Status(SLEEP, None, UNTIL_SLEEP, "locked"), now) is None  # paused


# ── the controller, with a stand-in for IOKit ────────────

class FakeIOKit:
    def __init__(self):
        self.calls = []
        self.held = set()

    refuse = None  # an IOKit error to refuse the assertion with

    def hold(self):
        aid = len(self.calls) + 1
        self.calls.append(("hold", aid))
        if self.refuse:
            return None, self.refuse
        self.held.add(aid)
        return aid, None

    def release(self, aid):
        self.calls.append(("release", aid))
        self.held.discard(aid)

    def declare_activity(self):
        self.calls.append(("declare",))


class FakeSettings:
    def __init__(self, **values):
        from settings import DEFAULTS

        self.values = {**DEFAULTS, **values}

    def get(self):
        return dict(self.values)


class Clock:
    def __init__(self, when):
        self.now = at(when)

    def __call__(self):
        return self.now


def controller(tmp_path, when="2026-11-21 10:00", **settings):
    iokit, clock, changes = FakeIOKit(), Clock(when), []
    ka = KeepAwake(FakeSettings(**settings), tmp_path, on_change=changes.append, iokit=iokit, clock=clock,
                   format_time=keepawake.clock_24, h24=True)  # a Mac set to 24-hour time
    return ka, iokit, clock, changes


def test_controller_holds_and_releases_one_assertion(tmp_path):
    ka, iokit, clock, changes = controller(tmp_path)
    assert ka.choose(HOUR) == {"choice": HOUR, "holding": True, "line": "Keep Awake ends at 11:00 (1 hour)"}
    clock.now += 21 * 60
    assert ka.status() == {"choice": HOUR, "holding": True, "line": "Keep Awake ends at 11:00 (39 minutes)"}  # counts down
    clock.now -= 21 * 60
    ka.choose(TWO_HOURS)  # a new pick while holding: still the one assertion
    assert iokit.calls == [("hold", 1)] and changes == [True]
    clock.now += 2 * 3600
    ka.feed({"has_battery": True, "external_connected": True, "battery_level": 80})
    assert iokit.calls == [("hold", 1), ("declare",), ("release", 1)] and changes == [True, False]
    assert ka.status() == {"choice": OFF, "holding": False, "line": None}


def test_controller_pauses_on_lock_without_declaring_activity(tmp_path):
    ka, iokit, clock, changes = controller(tmp_path)
    ka.choose(UNTIL_SLEEP)
    ka.environment(locked=True)
    ka.environment(locked=False)
    assert [c[0] for c in iokit.calls] == ["hold", "release", "hold"]
    ka.will_sleep()
    assert iokit.calls[-1][0] == "release" and not iokit.held and ("declare",) not in iokit.calls


def test_controller_hands_over_only_on_the_updaters_relaunch(tmp_path, monkeypatch):
    spawned, killed = [], []
    monkeypatch.setattr(keepawake, "_spawn_caffeinate", lambda seconds: spawned.append(seconds) or 4321)
    monkeypatch.setattr(keepawake, "_process_path", lambda pid: keepawake.CAFFEINATE if pid == 4321 else None)
    monkeypatch.setattr(keepawake.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    ka, iokit, clock, _ = controller(tmp_path)
    ka.choose(TWO_HOURS)
    ka.hand_over()
    assert iokit.calls[-1] == ("declare",)
    assert spawned == [600]  # caffeinate holds the screen on over the gap, at most 10 minutes
    path = tmp_path / keepawake.HANDOVER_FILE
    data = json.loads(path.read_text())
    assert (data["pick"], data["caffeinate"]) == (TWO_HOURS, 4321)

    new, new_iokit, new_clock, _ = controller(tmp_path)
    new_clock.now = clock.now + 300  # a slow first launch
    new._take_over()
    new.evaluate()
    new._end_bridge()
    assert new.status()["choice"] == TWO_HOURS and new_iokit.held
    assert killed == [(4321, keepawake.signal.SIGTERM)]
    assert not path.exists()  # consumed once: a later launch by hand brings back nothing

    again, again_iokit, _, _ = controller(tmp_path)
    again._take_over()
    again.evaluate()
    again._end_bridge()
    assert again.status()["choice"] == OFF and not again_iokit.held and len(killed) == 1


def test_a_reused_pid_is_never_ended(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(keepawake, "_process_path", lambda pid: "/Applications/Something.app/Contents/MacOS/Something")
    monkeypatch.setattr(keepawake.os, "kill", lambda pid, sig: killed.append(pid))
    clock = Clock("2026-11-21 10:00")
    (tmp_path / keepawake.HANDOVER_FILE).write_text(json.dumps({"written": clock.now, "caffeinate": 4321}))
    ka = KeepAwake(FakeSettings(), tmp_path, iokit=FakeIOKit(), clock=clock, format_time=keepawake.clock_24)
    ka._take_over()
    ka._end_bridge()
    assert killed == []


def test_handing_over_writes_nothing_with_nothing_to_carry(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(keepawake, "_spawn_caffeinate", lambda seconds: spawned.append(seconds) or 99)
    ka, iokit, clock, _ = controller(tmp_path)
    ka.evaluate()
    ka.hand_over()
    assert not (tmp_path / keepawake.HANDOVER_FILE).exists() and spawned == []
    # Work hours holding with no pick: only caffeinate's pid goes over, for the gap.
    ka, iokit, clock, _ = controller(tmp_path, "2026-11-19 16:50", keep_awake_hours=HOURS)
    ka.evaluate()
    ka.hand_over()
    data = json.loads((tmp_path / keepawake.HANDOVER_FILE).read_text())
    assert spawned == [600] and data == {"written": clock.now, "caffeinate": 99}


def test_a_refused_assertion_is_logged_once(tmp_path, caplog):
    ka, iokit, clock, _ = controller(tmp_path)
    iokit.refuse = 0xE00002C2
    with caplog.at_level("WARNING", logger="pythonflow"):
        ka.choose(UNTIL_SLEEP)
        for _ in range(5):
            ka.evaluate()
    assert [r.getMessage() for r in caplog.records] == ["Keep Awake couldn't hold the screen on (IOKit error 0xe00002c2)"]
    iokit.refuse = None
    ka.evaluate()
    assert iokit.held


def test_controller_follows_the_settings(tmp_path):
    ka, iokit, clock, changes = controller(tmp_path, "2026-11-19 10:00")
    ka.evaluate()
    assert not ka.holding
    ka._settings.values["keep_awake_hours"] = HOURS
    ka.settings_changed(ka._settings.values)
    assert ka.holding and ka.status() == {"choice": None, "holding": True,
                                          "line": "Keep Awake ends at 17:00 (7 hours)"}


def test_no_choice_or_line_reaches_the_page_before_launch(tmp_path):
    ka = KeepAwake(FakeSettings(), tmp_path)
    assert ka.status() == {"choice": OFF, "holding": False, "line": None}
    assert not os.path.exists(tmp_path / keepawake.HANDOVER_FILE)


def test_whether_the_screen_is_locked_is_read_again_as_the_mac_or_its_screens_wake(tmp_path, monkeypatch):
    """In case the lock's notification never came: waking to the lock screen mustn't keep it lit."""
    ka, iokit, clock, _ = controller(tmp_path)
    ka.choose(UNTIL_SLEEP)
    ka.environment(screens_awake=False)
    monkeypatch.setattr(keepawake, "_locked", lambda: (True, False))
    ka.environment(screens_awake=True)
    assert not ka.holding and ka._status.paused == "locked"
    monkeypatch.setattr(keepawake, "_locked", lambda: (False, False))
    ka.will_sleep(woke=True)  # Until Sleep is over by now anyway; nothing reads as locked
    assert not ka._env.locked


def test_lock_notifications_reach_keep_awake_through_their_observer():
    changes = []
    observer = keepawake._lock_observer()
    observer.on_change = lambda **change: changes.append(change)
    observer.screenLocked_(None)
    observer.screenUnlocked_(None)
    assert changes == [{"locked": True}, {"locked": False}]
    assert observer.respondsToSelector_("screenLocked:") and observer.respondsToSelector_("screenUnlocked:")


# ── Noon, Midnight and 24:00 ─────────────────────────────

TWELVE = lambda t: datetime.fromtimestamp(t).strftime("%-I:%M %p")
TWENTY_FOUR = lambda t: datetime.fromtimestamp(t).strftime("%H:%M")


def test_a_12_hour_mac_never_says_12_am_or_12_pm():
    label = keepawake.clock_label
    assert label(at("2026-11-19 12:00"), TWELVE, False) == "Noon"
    assert label(at("2026-11-19 00:00"), TWELVE, False) == "Midnight"
    assert label(at("2026-11-20 00:00"), TWELVE, False, end=True) == "Midnight"
    assert label(at("2026-11-19 12:30"), TWELVE, False) == "12:30 PM"  # only the hour itself is Noon
    assert label(at("2026-11-19 17:00"), TWELVE, False) == "5:00 PM"


def test_a_24_hour_mac_ends_the_day_at_24_00():
    label = keepawake.clock_label
    assert label(at("2026-11-20 00:00"), TWENTY_FOUR, True, end=True) == "24:00"
    assert label(at("2026-11-19 00:00"), TWENTY_FOUR, True) == "00:00"  # a start
    assert label(at("2026-11-19 12:00"), TWENTY_FOUR, True) == "12:00"


def test_the_bars_labels_come_from_the_same_helper():
    twelve = keepawake.half_hour_labels(TWELVE, False)
    assert len(twelve) == 49 and twelve[0] == "Midnight" and twelve[1] == "12:30 AM"
    assert twelve[24] == "Noon" and twelve[48] == "Midnight" and twelve[34] == "5:00 PM"
    assert keepawake.bar_labels(TWELVE, False) == ["Midnight", "3 AM", "6 AM", "9 AM", "Noon", "3 PM", "6 PM", "9 PM", "Midnight"]
    assert keepawake.bar_labels(TWENTY_FOUR, True) == ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00", "24:00"]


def test_the_line_says_noon_and_midnight(tmp_path):
    ka, iokit, clock, _ = controller(tmp_path, "2026-11-19 10:00", keep_awake_hours={**HOURS, "end": "12:00"})
    ka._format_time, ka._h24 = TWELVE, False
    ka.evaluate()
    assert ka.status()["line"] == "Keep Awake ends at Noon (2 hours)"
    ka._settings.values["keep_awake_hours"] = {**HOURS, "end": "24:00"}
    ka.evaluate()
    assert ka.status()["line"] == "Keep Awake ends at Midnight (14 hours)"
    ka._format_time, ka._h24 = TWENTY_FOUR, True
    assert ka.status()["line"] == "Keep Awake ends at 24:00 (14 hours)"
    assert ka.clock()["hours"][-1] == "24:00"


def test_which_clock_the_mac_uses():
    assert not keepawake.is_24_hour("h a") and not keepawake.is_24_hour("h a")
    assert keepawake.is_24_hour("HH") and keepawake.is_24_hour("H") and keepawake.is_24_hour("k")
    assert not keepawake.is_24_hour(None)


def test_work_hours_can_end_at_24_00_and_only_end_there():
    assert validate_hours({"start": "18:00", "end": "24:00"})["end"] == "24:00"
    assert validate_hours({"start": "24:00", "end": "24:00"})["start"] == "09:00"
    assert validate_hours({"start": "00:00", "end": "24:01"})["end"] == "17:00"
    late = {**HOURS, "start": "18:00", "end": "24:00"}
    assert block_end(late, at("2026-11-19 23:59")) == at("2026-11-20 00:00")
    assert block_end(late, at("2026-11-20 00:00")) is None  # Friday's block starts at 18:00
    m = Model()
    s, notes, _ = run(m, "2026-11-19 20:00", hours=late)
    assert notes == ["Keep Awake on until 24:00 (work hours)"]


def test_a_day_long_block_on_a_daylight_saving_day():
    sunday = {**HOURS, "start": "00:00", "end": "24:00", "days": [6]}
    assert block_end(sunday, at("2026-11-01 12:00")) == at("2026-11-02 00:00")
    assert at("2026-11-02 00:00") - at("2026-11-01 00:00") == 25 * 3600  # clocks went back that night
    assert block_end(sunday, at("2026-03-08 12:00")) == at("2026-03-09 00:00")
    assert at("2026-03-09 00:00") - at("2026-03-08 00:00") == 23 * 3600


# ── a pick takes over from that day's work hours ─────────

def test_a_timer_picked_before_work_hours_holds_on_its_own_terms_and_skips_the_day():
    m = Model()
    m.choose(TWO_HOURS, at("2026-11-19 08:00"), HOURS)
    s, notes, _ = run(m, "2026-11-19 08:00")
    assert (s.kind, s.until, s.lit) == (TIMER, at("2026-11-19 10:00"), TWO_HOURS)
    s, notes, _ = run(m, "2026-11-19 09:00")
    assert (s.kind, s.until, notes) == (TIMER, at("2026-11-19 10:00"), [])  # work hours don't take over
    s, notes, declare = run(m, "2026-11-19 10:00")
    assert (s.holding, s.lit, s.off_today) == (False, OFF, True) and declare
    assert notes == ["Keep Awake off (2 hours are up, for the rest of today's work hours)"]
    assert sentence(s, 0) is None
    assert not status(m, "2026-11-19 14:00").holding
    assert status(m, "2026-11-20 09:00").kind == SCHEDULE  # back the next workday


def test_a_short_timer_that_ends_before_work_hours_still_skips_them():
    m = Model()
    m.choose(HOUR, at("2026-11-19 07:00"), HOURS)
    run(m, "2026-11-19 07:00")
    s, notes, _ = run(m, "2026-11-19 08:00")
    assert notes == ["Keep Awake off (1 hour is up, for the rest of today's work hours)"]
    assert not status(m, "2026-11-19 09:30").holding


def test_off_before_work_hours_means_none_that_day():
    m = Model()
    m.choose(OFF, at("2026-11-19 08:00"), HOURS)
    s, notes, _ = run(m, "2026-11-19 08:00")
    assert (s.holding, s.lit, notes) == (False, OFF, [])  # nothing was holding the screen on
    assert not status(m, "2026-11-19 09:00").holding
    assert not status(m, "2026-11-19 16:59").holding
    assert status(m, "2026-11-20 09:00").holding


def test_until_sleep_picked_before_work_hours_ends_at_sleep_and_skips_the_day():
    m = Model()
    m.choose(UNTIL_SLEEP, at("2026-11-19 08:00"), HOURS)
    assert status(m, "2026-11-19 12:00").kind == SLEEP
    m.slept()
    s, notes, _ = run(m, "2026-11-19 13:00")
    assert not s.holding and s.off_today
    assert notes == ["Keep Awake off (the Mac is going to sleep, for the rest of today's work hours)"]


def test_a_pick_after_work_hours_have_ended_changes_nothing_about_them():
    m = Model()
    m.choose(OFF, at("2026-11-19 18:00"), HOURS)
    assert m.off_day is None
    m.choose(HOUR, at("2026-11-19 18:00"), HOURS)
    s = status(m, "2026-11-19 18:00")
    assert (s.kind, s.until, s.off_today) == (TIMER, at("2026-11-19 19:00"), False)
    assert status(m, "2026-11-20 09:00").kind == SCHEDULE


def test_picks_on_a_day_off_or_with_work_hours_off_leave_them_alone():
    m = Model()
    m.choose(OFF, at("2026-11-21 08:00"), HOURS)  # a Saturday
    assert m.off_day is None and status(m, "2026-11-23 09:00").kind == SCHEDULE  # Monday
    m = Model()
    m.choose(HOUR, at("2026-11-19 08:00"), NO_HOURS)
    run(m, "2026-11-19 08:00", hours=NO_HOURS)
    assert m.off_day is None
    s, notes, _ = run(m, "2026-11-19 10:00")  # turned on inside the block, after the hour: it takes effect
    assert s.kind == SCHEDULE and notes == ["Keep Awake on until 17:00 (work hours)"]


def test_turning_work_hours_on_inside_the_block_takes_effect_even_after_a_pick_that_day():
    m = Model()
    m.choose(OFF, at("2026-11-19 10:00"), HOURS)
    run(m, "2026-11-19 10:00")
    run(m, "2026-11-19 10:01", hours=NO_HOURS)
    s = status(m, "2026-11-19 10:02")  # back on
    assert s.kind == SCHEDULE and s.holding


def test_until_sleep_from_the_evening_before_runs_into_work_hours_and_on_past_them():
    m = Model()
    m.choose(UNTIL_SLEEP, at("2026-11-18 20:00"), HOURS)  # Wednesday evening, after its work hours
    assert m.off_day is None
    s = status(m, "2026-11-19 09:30")  # never slept
    assert (s.kind, s.lit) == (SLEEP, UNTIL_SLEEP)
    assert status(m, "2026-11-19 18:00").kind == SLEEP
    m.slept()
    assert status(m, "2026-11-20 09:00").kind == SCHEDULE


def test_a_timer_from_the_night_before_meets_the_block_under_the_old_rule_with_no_tile_lit():
    night = {**HOURS, "start": "00:00", "end": "06:00", "days": [3]}  # Thursday 0:00–6:00
    m = Model()
    m.choose(TWO_HOURS, at("2026-11-18 23:30"), night)  # Wednesday: not one of its days
    s, notes, _ = run(m, "2026-11-18 23:30", hours=night)
    assert (s.kind, s.lit) == (TIMER, TWO_HOURS)
    s, notes, _ = run(m, "2026-11-19 00:00", hours=night)
    assert (s.kind, s.until, s.lit) == (SCHEDULE, at("2026-11-19 06:00"), None)  # whichever ends later
    assert notes == ["Keep Awake on until 06:00 (work hours)"]
