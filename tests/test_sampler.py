"""Sampling rate: fast while the dashboard page is polling, slow otherwise; what the sampler's
waits tell the app's priority (no thread is started)."""

import logging

import main
from main import BACKGROUND_INTERVAL, DASHBOARD_IDLE_AFTER, DASHBOARD_INTERVAL, Sampler, first_reading_note


def test_rate_follows_dashboard_polls(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    sampler = Sampler(reader=None, on_sample=None)
    assert sampler.interval() == BACKGROUND_INTERVAL

    sampler.dashboard_polled()
    assert sampler.interval() == DASHBOARD_INTERVAL
    assert sampler._wake.is_set()  # woken so the dashboard gets a fresh sample straight away

    sampler._wake.clear()
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled()
    assert not sampler._wake.is_set()  # already fast: a poll doesn't trigger extra samples

    now[0] += DASHBOARD_IDLE_AFTER
    assert sampler.interval() == BACKGROUND_INTERVAL


class _Priority:
    def __init__(self):
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


def test_the_first_poll_of_a_watched_dashboard_raises_the_priority_before_it_wakes_the_sampler(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    priority = _Priority()
    sampler = Sampler(reader=None, on_sample=None, priority=priority)
    sampler._wake = type("Wake", (), {"set": lambda self: seen.append(priority.refreshed)})()
    seen = []
    sampler.dashboard_polled()
    assert priority.refreshed == 1 and seen == [1]  # refreshed first, then woken
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled()  # already watched: nothing changes
    assert priority.refreshed == 1
    now[0] += DASHBOARD_IDLE_AFTER
    sampler.dashboard_polled()  # watched again after a gap
    assert priority.refreshed == 2


def test_the_first_reading_is_only_logged_when_it_was_slow():
    assert first_reading_note(0.8) is None
    assert first_reading_note(2.0) is None
    assert first_reading_note(62.44) == "First reading 62.4 s after launch"


def test_the_log_says_when_the_wait_was_before_the_app_began_to_run():
    """A copy that has never run is held at exec by macOS's security scan: 224 s on the M1 Pro, after a compile."""
    assert first_reading_note(224.0, began=223.5) == "First reading 224.0 s after launch (223.5 s of it before the app's code began to run)"
    assert first_reading_note(3.1, began=0.6) == "First reading 3.1 s after launch"  # an ordinary start: the app's own wait
    assert first_reading_note(1.2, began=0.6) is None
    assert first_reading_note(5.0, began=None) == "First reading 5.0 s after launch"


def test_the_log_says_when_the_first_reading_was_read():
    """#59: read at 9.6 s, but the main thread couldn't put it in the menu bar until 22.2 s."""
    assert first_reading_note(22.2, began=1.0, read=9.6) == "First reading 22.2 s after launch (read at 9.6 s)"
    assert first_reading_note(224.0, began=223.5, read=223.9) == (
        "First reading 224.0 s after launch (read at 223.9 s; 223.5 s of it before the app's code began to run)")
    assert first_reading_note(1.5, read=1.4) is None


def test_the_first_reading_shown_takes_when_it_was_read_to_the_log(monkeypatch, caplog):
    from types import SimpleNamespace

    from main import EngineRoomApp

    monkeypatch.setattr(main, "_seconds_since_launch", lambda: 22.2)
    monkeypatch.setattr(main, "_began", 1.0)
    monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
    shown = []
    app = SimpleNamespace(_statusbar=SimpleNamespace(update=lambda *reading: shown.append(reading)),
                          _first_reading_seen=False, _check_graphics_process=lambda: None, _quitting=False)
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        EngineRoomApp._show_sample(app, {"t": 1}, None, {}, 1000.0 - 12.6)
        EngineRoomApp._show_sample(app, {"t": 2}, None, {}, 1000.0)  # only the first one says anything
    assert [r.getMessage() for r in caplog.records] == ["First reading 22.2 s after launch (read at 9.6 s)"]
    assert len(shown) == 2


def test_late_wakeups_dont_stretch_the_interval(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    sampler = Sampler(reader=None, on_sample=None)
    due = now[0]
    for _ in range(10):
        due = sampler.next_due(due)
        now[0] = due + 0.09  # each wait ends 90 ms late, as at background QoS
    assert due == 1000.0 + 10 * BACKGROUND_INTERVAL

    now[0] += 60  # far behind (a slow sample): no burst of catch-up samples
    assert sampler.next_due(due) == now[0] + 0.05


class _Reader:
    def __init__(self):
        self.parts = []

    def read(self, parts=None):
        self.parts.append(parts)
        return {"t": 1.0}


def test_thermals_read_only_the_parts_asked_for():
    wanted = [()]
    seen = []
    power, thermals = _Reader(), _Reader()
    sampler = Sampler(reader=power, on_sample=lambda s, t: seen.append(t), thermals=thermals,
                      thermal_parts=lambda: wanted[0])
    sampler.sample()
    wanted[0] = ("cpu",)
    sampler.sample()
    assert thermals.parts == [("cpu",)] and seen == [None, {"t": 1.0}]


def test_a_page_with_fans_wakes_the_sampler_once(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    sampler = Sampler(reader=None, on_sample=None)
    sampler.dashboard_polled()
    sampler._wake.clear()
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled(fans=True)
    assert sampler._wake.is_set() and sampler.fans_watched()  # fans now, not in a second
    sampler._wake.clear()
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled(fans=True)
    assert not sampler._wake.is_set()
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled(fans=False)  # switched to Power: stops once it's been a while
    now[0] += DASHBOARD_IDLE_AFTER
    assert not sampler.fans_watched()


def test_temperatures_are_always_read_and_fans_only_while_shown():
    """Like power, so the chart has no gaps when the page is covered or another tab shows."""
    import thermals
    from types import SimpleNamespace

    from main import EngineRoomApp

    state = {"watched": False}
    app = SimpleNamespace(_sampler=SimpleNamespace(fans_watched=lambda: state["watched"]),
                          _statusbar=SimpleNamespace(menu_open=False), _fans=SimpleNamespace(holding=False))
    assert EngineRoomApp._thermal_parts(app) == thermals.TEMPERATURES
    app._statusbar.menu_open = True
    assert EngineRoomApp._thermal_parts(app) == thermals.ALL_PARTS
    app._statusbar.menu_open, state["watched"] = False, True
    assert EngineRoomApp._thermal_parts(app) == thermals.ALL_PARTS
    state["watched"], app._fans.holding = False, True  # fan control holds them: the safety sensors too
    assert EngineRoomApp._thermal_parts(app) == thermals.ALL_PARTS | {thermals.SAFETY}


def test_a_fan_profile_samples_every_second(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    fans = {"running": False}
    sampler = Sampler(reader=None, on_sample=None, fans_running=lambda: fans["running"])
    assert sampler.interval() == BACKGROUND_INTERVAL
    fans["running"] = True  # the curve runs once a second
    assert sampler.interval() == DASHBOARD_INTERVAL
