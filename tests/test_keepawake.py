"""Keep Awake's rules (the pure Model), its settings, its sentence, and the controller with a
stand-in for IOKit. Times are local: each test runs in a fixed time zone."""

import json
import os
import time
from datetime import datetime

import pytest

import keepawake
from keepawake import (HOUR, OFF, SCHEDULE, SLEEP, TIMER, TWO_HOURS, UNTIL_SLEEP, Env, KeepAwake, Model,
                       Status, block_end, duration_words, first_weekday, sentence, validate_battery,
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


def test_off_outside_work_hours_just_turns_it_off():
    m = Model()
    m.choose(HOUR, at("2026-11-19 07:00"), HOURS)
    run(m, "2026-11-19 07:00")
    m.choose(OFF, at("2026-11-19 07:30"), HOURS)
    s, notes, declare = run(m, "2026-11-19 07:30")
    assert (s.holding, s.lit, s.off_today) == (False, OFF, False)
    assert notes == ["Keep Awake off (Off chosen)"]
    assert status(m, "2026-11-19 09:00").kind == SCHEDULE  # the block still starts at nine


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


def test_one_hour_inside_the_block_holds_until_whichever_ends_later():
    m = Model()
    m.choose(HOUR, at("2026-11-19 16:30"), HOURS)
    s, notes, _ = run(m, "2026-11-19 16:30")
    assert (s.kind, s.until, s.lit) == (TIMER, at("2026-11-19 17:30"), HOUR)
    assert notes == ["Keep Awake on for 1 hour (until 17:30)"]
    assert run(m, "2026-11-19 17:00")[0].holding  # the block's over; the hour isn't
    s, notes, _ = run(m, "2026-11-19 17:30")
    assert not s.holding and notes == ["Keep Awake off (1 hour is up)"]

    m = Model()
    run(m, "2026-11-19 09:30")
    m.choose(HOUR, at("2026-11-19 10:00"), HOURS)
    s, notes, _ = run(m, "2026-11-19 10:00")
    assert (s.kind, s.until, s.lit) == (SCHEDULE, at("2026-11-19 17:00"), HOUR)
    assert notes == ["Keep Awake on until 17:00 (work hours); 1 hour chosen, until 11:00"]
    s, notes, _ = run(m, "2026-11-19 11:00")
    assert (s.holding, s.lit, notes) == (True, None, [])  # the hour's up; the block carries on


def test_a_timer_running_into_the_block_hands_over_to_it():
    m = Model()
    m.choose(HOUR, at("2026-11-19 08:30"), HOURS)
    run(m, "2026-11-19 08:30")
    s, notes, _ = run(m, "2026-11-19 09:00")
    assert (s.kind, s.until) == (SCHEDULE, at("2026-11-19 17:00"))
    assert notes == ["Keep Awake on until 17:00 (work hours)"]


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
    assert sentence(s, at("2026-11-19 10:00")) == "Screen stays on for 1 more hour."
    s, notes, declare = run(m, "2026-11-19 11:05")
    assert (s.holding, s.lit, s.off_today) == (False, OFF, True)
    assert notes == ["Keep Awake off (1 hour is up, for the rest of today's work hours)"] and declare
    assert sentence(s, at("2026-11-19 11:05")) == "Off for the rest of today's work hours."
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
    assert sentence(s, at("2026-11-19 13:30")) == "Off for the rest of today's work hours."


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
    assert sentence(s, at("2026-11-21 10:10")) == "Paused because the battery is below 20%."
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
    assert not s.battery_low and sentence(s, at("2026-11-21 10:00")) is None


# ── the sentence ─────────────────────────────────────────

def test_minutes_and_hours_as_whole_sentences():
    assert duration_words(42 * 60) == "42 more minutes"
    assert duration_words(41 * 60 + 1) == "42 more minutes"  # rounded up: never "0"
    assert duration_words(59) == "1 more minute"
    assert duration_words(0) == "1 more minute"
    assert duration_words(3600) == "1 more hour"
    assert duration_words(7200) == "2 more hours"
    assert duration_words(72 * 60) == "1 more hour and 12 minutes"
    assert duration_words(61 * 60) == "1 more hour and 1 minute"
    assert duration_words(119 * 60 + 30) == "2 more hours"


def test_the_sentence_for_each_state():
    now = at("2026-11-19 16:18")
    twelve = lambda t: datetime.fromtimestamp(t).strftime("%-I:%M %p")
    assert sentence(Status(TIMER, now + 42 * 60, HOUR), now) == "Screen stays on for 42 more minutes."
    assert sentence(Status(SLEEP, None, UNTIL_SLEEP), now) == "Screen stays on until the Mac sleeps."
    assert (sentence(Status(SCHEDULE, at("2026-11-19 17:00")), now, twelve)
            == "Screen stays on until 5:00 PM (work hours).")
    assert sentence(Status(SCHEDULE, at("2026-11-19 17:00")), now) == "Screen stays on until 17:00 (work hours)."
    assert sentence(Status(off_today=True), now) == "Off for the rest of today's work hours."
    assert sentence(Status(TIMER, now + 60, HOUR, "battery", battery_low=True, level=30), now) == (
        "Paused because the battery is below 30%.")
    assert sentence(Status(), now) is None
    # Locked, or the screens asleep: the usual line (nobody can see the menu then anyway).
    assert sentence(Status(SLEEP, None, UNTIL_SLEEP, "locked"), now) == "Screen stays on until the Mac sleeps."


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
                   format_time=keepawake.clock_24)
    return ka, iokit, clock, changes


def test_controller_holds_and_releases_one_assertion(tmp_path):
    ka, iokit, clock, changes = controller(tmp_path)
    assert ka.choose(HOUR) == {"choice": HOUR, "holding": True, "line": "Screen stays on for 1 more hour."}
    assert ka.status() == {"choice": HOUR, "holding": True, "line": "Screen stays on for 1 more hour."}
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
                                          "line": "Screen stays on until 17:00 (work hours)."}


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
