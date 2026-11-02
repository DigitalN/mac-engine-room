"""Sampling rate and QoS: fast while the dashboard page is polling, slow and on the
efficiency cores otherwise (no thread is started)."""

import main
from main import (BACKGROUND_INTERVAL, DASHBOARD_IDLE_AFTER, DASHBOARD_INTERVAL, QOS_CLASS_BACKGROUND,
                  QOS_CLASS_UTILITY, Sampler)


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


def test_qos_follows_dashboard_polls(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    sampler = Sampler(reader=None, on_sample=None)
    assert sampler.qos() == QOS_CLASS_BACKGROUND
    sampler.dashboard_polled()
    assert sampler.qos() == QOS_CLASS_UTILITY
    now[0] += DASHBOARD_IDLE_AFTER
    assert sampler.qos() == QOS_CLASS_BACKGROUND


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


def test_the_thermals_page_wakes_the_sampler_once(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    sampler = Sampler(reader=None, on_sample=None)
    sampler.dashboard_polled()
    sampler._wake.clear()
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled(thermals=True)
    assert sampler._wake.is_set() and sampler.thermals_watched()  # temperatures now, not in a second
    sampler._wake.clear()
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled(thermals=True)
    assert not sampler._wake.is_set()
    now[0] += DASHBOARD_INTERVAL
    sampler.dashboard_polled(thermals=False)  # switched to Power: stops once it's been a while
    now[0] += DASHBOARD_IDLE_AFTER
    assert not sampler.thermals_watched()


def test_temperatures_are_always_read_and_fans_only_while_shown():
    """Like power, so the chart has no gaps when the page is covered or another tab shows."""
    import thermals
    from types import SimpleNamespace

    from main import EngineRoomApp

    state = {"watched": False}
    app = SimpleNamespace(_sampler=SimpleNamespace(thermals_watched=lambda: state["watched"]),
                          _statusbar=SimpleNamespace(menu_open=False))
    assert EngineRoomApp._thermal_parts(app) == thermals.TEMPERATURES
    app._statusbar.menu_open = True
    assert EngineRoomApp._thermal_parts(app) == thermals.ALL_PARTS
    app._statusbar.menu_open, state["watched"] = False, True
    assert EngineRoomApp._thermal_parts(app) == thermals.ALL_PARTS
