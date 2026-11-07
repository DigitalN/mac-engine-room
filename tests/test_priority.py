"""One priority for the app's threads: when the Mac counts as busy or quiet (the pure `Load`),
and how the threads are raised and lowered (`Priority`, against stand-ins for the system calls).
One test makes the real calls on a thread of its own."""

import logging
import threading

import pytest

import priority
from priority import BUSY, LAUNCH, QUIET, CpuIdle, Load, Priority


def spare_for(load, start, seconds, step=2.5, share=0.9):
    """Feeds spare-CPU readings every `step` seconds; returns the time of the last one."""
    t = start
    while t <= start + seconds:
        load.idle(t, share)
        t += step
    return t - step


# ── Load ──

def test_a_launch_stays_at_full_priority_until_the_cpu_has_been_spare_for_30_seconds():
    load = Load()
    assert load.state == LAUNCH and load.full
    last = spare_for(load, 0, 27.5)
    assert load.full and load.state == LAUNCH  # 27.5 s of spare CPU is not enough
    load.idle(last + 2.5, 0.9)  # 30 s
    assert load.state == QUIET and not load.full


def test_a_launch_on_a_busy_mac_is_busy_from_the_first_reading():
    load = Load()
    load.idle(2.5, 0.02)  # a login: every core busy
    assert load.state == BUSY and load.full


def test_waits_ending_more_than_a_quarter_second_late_mean_busy():
    load = Load()
    spare_for(load, 0, 40)
    assert load.state == QUIET
    load.waited(50.0, 0.09)  # background waits normally end ~90 ms late
    load.waited(52.5, 0.25)
    assert load.state == QUIET
    load.waited(55.0, 0.26)
    assert load.state == BUSY and load.full


def test_busy_ends_after_30_seconds_with_spare_cpu_and_any_busy_reading_starts_the_count_again():
    load = Load()
    load.waited(0.0, 1.6)
    last = spare_for(load, 1, 25)
    assert load.state == BUSY
    load.idle(last + 2.5, 0.1)  # a burst of work
    assert load.state == BUSY
    last = spare_for(load, last + 5, 27.5)
    assert load.state == BUSY  # counting from the burst, not before it
    load.idle(last + 2.5, 0.9)
    assert load.state == QUIET


def test_a_late_wait_while_busy_starts_the_count_again():
    load = Load()
    load.waited(0.0, 1.0)
    load.idle(1.0, 0.9)
    load.waited(20.0, 0.5)
    load.idle(31.0, 0.9)  # 30 s after the first spare reading, but a wait was late in between
    assert load.state == BUSY
    load.idle(61.0, 0.9)
    assert load.state == QUIET


def test_spare_cpu_means_a_quarter_of_the_cores_idle():
    load = Load()
    load.idle(0.0, 0.25)  # exactly a quarter: spare
    load.idle(30.0, 0.25)
    assert load.state == QUIET
    load.idle(31.0, 0.24)
    assert load.state == BUSY


def test_a_cpu_that_cant_be_read_counts_as_spare_so_the_app_cant_get_stuck_at_full_priority():
    load = Load()
    load.idle(0.0, None)
    load.idle(30.0, None)
    assert load.state == QUIET


# ── CpuIdle ──

def test_idle_share_comes_from_the_change_in_tick_counts_and_survives_a_wrap():
    readings = iter([(100, 100, 800, 0), (110, 100, 890, 0), (110 + 90, 100 + 10, 890 + 0, 0), (5, 3, 2, 0)])
    clock = iter(range(0, 100, 2))
    idle = CpuIdle(read=lambda: next(readings), now=lambda: next(clock))
    assert idle.share() == pytest.approx(0.9)  # 90 of 100 ticks
    assert idle.share() == pytest.approx(0.0)  # 100 ticks, none idle
    # the counters are 32-bit: the next reading is past the wrap
    assert idle.share() == pytest.approx((2 - 890) % 2**32 / ((5 - 200) % 2**32 + (3 - 110) % 2**32 + (2 - 890) % 2**32))


def test_idle_share_is_unknown_when_nothing_was_read_or_nothing_passed():
    clock = iter(range(0, 100, 2))
    idle = CpuIdle(read=lambda: None, now=lambda: next(clock))
    assert idle.share() is None
    same = CpuIdle(read=lambda: (1, 1, 1, 1), now=lambda: next(clock))
    assert same.share() is None


def test_a_window_under_a_second_is_too_short_to_read_and_keeps_growing():
    now = [0.0]
    readings = {0.0: (0, 0, 0, 0), 1.0: (10, 10, 70, 10)}
    idle = CpuIdle(read=lambda: readings[now[0]], now=lambda: now[0])
    now[0] = 0.075  # the first sample of a launch
    assert idle.share() is CpuIdle.NOT_YET
    now[0] = 1.0
    assert idle.share() == pytest.approx(0.7)  # counted from the start, not from the short look


# ── Priority ──

class Stand_ins:
    """Records what the threads were told, in place of the system calls."""

    def __init__(self):
        self.qos = []
        self.raised, self.lowered = [], []
        self.fail = False

    def set_qos(self, qos_class):
        self.qos.append(qos_class)

    def raise_(self, ident):
        self.raised.append(ident)
        return None if self.fail else 1000 + len(self.raised)

    def lower(self, handle):
        self.lowered.append(handle)


@pytest.fixture
def world():
    state = {"in_use": False, "now": 0.0, "share": 0.9}
    calls = Stand_ins()
    idle = CpuIdle(read=lambda: (0, 0, 0, 0))
    idle.share = lambda: state["share"]
    p = Priority(in_use=lambda: state["in_use"], now=lambda: state["now"], idle=idle,
                 set_qos=calls.set_qos, raise_=calls.raise_, lower=calls.lower)
    return p, state, calls


def run_in_thread(fn):
    """Runs `fn` on a thread of its own and returns that thread's ident."""
    out = []
    t = threading.Thread(target=lambda: out.append(threading.get_ident()) or fn())
    t.start()
    t.join()
    return out[0]


def keep_registered(p, count):
    """Starts `count` threads that register and stay alive (a finished thread's id gets reused);
    returns a function that ends them."""
    ready, release = threading.Barrier(count + 1), threading.Event()

    def body():
        p.register()
        ready.wait(5)
        release.wait(5)

    threads = [threading.Thread(target=body) for _ in range(count)]
    for t in threads:
        t.start()
    ready.wait(5)

    def end():
        release.set()
        for t in threads:
            t.join()

    return end


def test_a_thread_registers_at_background_qos_and_starts_at_full_priority(world):
    p, state, calls = world
    ident = run_in_thread(p.register)
    assert calls.qos == [priority.QOS_CLASS_BACKGROUND]
    assert calls.raised == [ident]  # launch: full priority


def test_threads_drop_to_background_once_the_mac_is_quiet_and_nothing_is_open(world, caplog):
    p, state, calls = world
    end = keep_registered(p, 2)
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        for state["now"] in (0, 15, 30, 31):
            p.sampled()
    assert p.load.state == QUIET
    assert len(calls.lowered) == 2 and len(calls.raised) == 2  # both overrides ended, none started again
    end()
    assert "Mac busy" not in caplog.text and "Mac quiet again" not in caplog.text  # a quiet launch says nothing


def test_the_menu_or_a_watched_dashboard_raises_every_thread_and_closing_it_lowers_them(world):
    p, state, calls = world
    run_in_thread(p.register)
    for state["now"] in (0, 15, 30, 31):
        p.sampled()
    assert len(calls.lowered) == 1
    state["in_use"] = True  # the menu opens
    p.refresh()
    assert len(calls.raised) == 2
    p.refresh()  # nothing changed: nothing is called again
    assert len(calls.raised) == 2
    state["in_use"] = False
    p.refresh()
    assert len(calls.lowered) == 2


def test_a_thread_registering_while_the_menu_is_open_is_raised_at_once(world):
    p, state, calls = world
    for state["now"] in (0, 15, 30, 31):
        p.sampled()
    state["in_use"] = True
    p.refresh()
    ident = run_in_thread(p.register)
    assert calls.raised == [ident]
    run_in_thread(lambda: None)


def test_a_late_wait_raises_the_threads_and_the_log_says_so_and_says_when_it_is_quiet_again(world, caplog):
    p, state, calls = world
    run_in_thread(p.register)
    for state["now"] in (0, 15, 30, 31):
        p.sampled()
    assert p.load.state == QUIET
    raised = len(calls.raised)
    with caplog.at_level(logging.INFO, logger="pythonflow"):
        state["now"] = 60.0
        p.waited(1.4)
        assert p.load.state == BUSY and len(calls.raised) == raised + 1
        for state["now"] in (62.5, 75, 90, 92.5):
            p.sampled()
    assert p.load.state == QUIET
    assert [r.getMessage() for r in caplog.records] == ["Mac busy: running at full priority", "Mac quiet again"]


def test_the_cpu_is_not_read_while_quiet(world):
    p, state, calls = world
    reads = []
    p._idle.share = lambda: reads.append(1) or 0.9
    for state["now"] in (0, 15, 30):
        p.sampled()
    assert p.load.state == QUIET and len(reads) == 3
    for state["now"] in (31, 33.5, 36, 38.5):
        p.sampled()
    assert len(reads) == 3  # quiet: nothing read


def test_a_thread_that_ends_gives_back_its_override_and_is_forgotten(world):
    p, state, calls = world
    ident = run_in_thread(lambda: (p.register(), p.unregister()))
    assert calls.raised == [ident] and calls.lowered == [1001]
    for state["now"] in (0, 15, 30, 31):
        p.sampled()
    state["in_use"] = True
    p.refresh()
    assert len(calls.raised) == 1  # it isn't touched any more


def test_a_failed_override_is_tried_again_at_the_next_switch(world):
    p, state, calls = world
    calls.fail = True
    run_in_thread(p.register)
    calls.fail = False
    for state["now"] in (0, 15, 30, 31):
        p.sampled()
    state["in_use"] = True
    p.refresh()
    assert len(calls.raised) == 2 and len(calls.lowered) == 0


# ── the real calls ──

def test_the_real_calls_raise_and_lower_a_thread_that_is_waiting():
    ready, release, done = threading.Event(), threading.Event(), threading.Event()
    box = {}

    def worker():
        priority.set_own_qos(priority.QOS_CLASS_BACKGROUND)
        box["ident"] = threading.get_ident()
        ready.set()
        release.wait()
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    ready.wait(5)
    handle = priority.raise_thread(box["ident"])
    assert handle
    priority.lower_thread(handle)
    release.set()
    assert done.wait(5)
    t.join()
    assert priority.cpu_ticks() is not None
