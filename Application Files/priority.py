"""Keeping Mac Engine Room responsive when every core is busy.

The main thread, the sampler and fan control share one Python interpreter lock. CPython makes
the thread holding it wait until a thread that has asked for it for 5 ms actually runs, so one
starved thread holds the others up: they have to run at the same priority, whatever it is.

At background QoS a thread stays on the efficiency cores, which is what keeps the app's idle
cost down. But with every core busy, a background thread waited up to 26 s for a turn
(measured on an M1 Pro, with 12 busy processes on 10 cores), against 5 ms at user-interactive.
So:

- The three threads sit at background QoS (they set it themselves, `register()`), and full
  priority is a user-interactive override on each of them, which any thread can start or end
  at once, even on a thread that is blocked.
- Full priority: while the menu is open or the dashboard is being watched, from launch until
  the Mac has been quiet, and while it's busy.
- Busy: a timed wait of the sampler ended more than `Load.LATE` after it was due. At background
  QoS they normally end 80–100 ms late.
- Quiet again: `Load.QUIET_FOR` seconds with `Load.SPARE` of the CPU idle, which says nothing
  about the threads at full priority, so it comes from the scheduler's tick counts.

`Load` is the pure part and is unit-tested.
"""

import ctypes
import logging
import threading
import time
from ctypes import POINTER, Structure, byref, c_int, c_uint, c_uint32, c_void_p

log = logging.getLogger("pythonflow")

# <sys/qos.h>
QOS_CLASS_USER_INTERACTIVE = 0x21
QOS_CLASS_BACKGROUND = 0x09

LAUNCH, BUSY, QUIET = "launch", "busy", "quiet"


class Load:
    """Whether the Mac is busy enough to starve the app's threads at background QoS. Starts
    at launch, at full priority, as busy as the Mac turns out to be: the first minute of a
    login launch took over a minute to show a reading at background QoS."""

    LATE = 0.25  # seconds a timed wait may end after it was due; background waits end ~0.09 s late
    SPARE = 0.25  # share of all the cores that must be idle to count as spare CPU
    QUIET_FOR = 30.0  # seconds of spare CPU before the Mac is quiet again

    def __init__(self):
        self.state = LAUNCH
        self._spare_since = None

    @property
    def full(self):
        """Whether the threads should be at full priority."""
        return self.state != QUIET

    def waited(self, now, late):
        """A timed wait ended `late` seconds after it was due."""
        if late > self.LATE:
            self._spare_since = None
            self.state = BUSY

    def idle(self, now, share):
        """`share` of the CPU was idle since the last reading (None: it couldn't be read, which
        counts as spare: the waits still say when the Mac is busy)."""
        if share is not None and share < self.SPARE:
            self._spare_since = None
            self.state = BUSY
        elif self.state == QUIET:
            return
        elif self._spare_since is None:
            self._spare_since = now
        elif now - self._spare_since >= self.QUIET_FOR:
            self.state = QUIET


# ── the system calls ──

_libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libsystem.pthread_set_qos_class_self_np.argtypes = [c_uint, c_int]
_libsystem.pthread_override_qos_class_start_np.argtypes = [c_void_p, c_uint, c_int]
_libsystem.pthread_override_qos_class_start_np.restype = c_void_p
_libsystem.pthread_override_qos_class_end_np.argtypes = [c_void_p]
_libsystem.host_statistics.argtypes = [c_uint32, c_int, c_void_p, POINTER(c_uint32)]
_libsystem.mach_host_self.restype = c_uint32
_HOST = _libsystem.mach_host_self()
_HOST_CPU_LOAD_INFO, _CPU_STATE_IDLE = 3, 2


class _CpuLoadInfo(Structure):
    _fields_ = [("ticks", c_uint32 * 4)]  # user, system, idle, nice; counts of all the cores together


def set_own_qos(qos_class):
    """Set the calling thread's QoS. Only a thread can change its own."""
    err = _libsystem.pthread_set_qos_class_self_np(qos_class, 0)
    if err:
        log.warning("Couldn't set thread QoS %#x (error %d)", qos_class, err)


def raise_thread(ident):
    """Start a user-interactive override on a thread (its `threading` ident is its pthread_t).
    It takes effect at once, whatever the thread is doing. Returns the override, or None."""
    handle = _libsystem.pthread_override_qos_class_start_np(ident, QOS_CLASS_USER_INTERACTIVE, 0)
    if not handle:
        log.warning("Couldn't raise a thread's priority")
    return handle or None


def lower_thread(handle):
    _libsystem.pthread_override_qos_class_end_np(handle)


def cpu_ticks():
    info, count = _CpuLoadInfo(), c_uint32(4)
    if _libsystem.host_statistics(_HOST, _HOST_CPU_LOAD_INFO, byref(info), byref(count)) != 0:
        return None
    return tuple(info.ticks)


class CpuIdle:
    """The share of all the cores that was idle since the last reading. The counts are in 10 ms
    ticks per core, too coarse for a window under `MIN_WINDOW` seconds: a reading before that
    is `NOT_YET`, and the window goes on growing."""

    MIN_WINDOW = 0.9
    NOT_YET = object()

    def __init__(self, read=cpu_ticks, now=time.monotonic):
        self._read, self._now = read, now
        self.restart()

    def restart(self):
        """The next reading counts from now."""
        self._last, self._last_at = self._read(), self._now()

    def share(self):
        at = self._now()
        if at - self._last_at < self.MIN_WINDOW:
            return self.NOT_YET
        ticks, last = self._read(), self._last
        self._last, self._last_at = ticks, at
        if ticks is None or last is None:
            return None
        change = [(a - b) & 0xFFFFFFFF for a, b in zip(ticks, last)]  # the counts wrap
        return change[_CPU_STATE_IDLE] / sum(change) if sum(change) else None


class Priority:
    """Puts the threads that `register()` at full priority or at background QoS, as `Load` and
    `in_use` say. Safe to call from any thread."""

    def __init__(self, in_use=lambda: False, now=time.monotonic, load=None, idle=None,
                 set_qos=set_own_qos, raise_=raise_thread, lower=lower_thread):
        self.load = load or Load()
        self._in_use = in_use
        self._now = now
        self._idle = idle or CpuIdle(now=now)
        self._set_qos, self._raise, self._lower = set_qos, raise_, lower
        self._lock = threading.Lock()
        self._threads = {}  # thread ident -> its override while it's at full priority
        self._full = True  # threads begin at their ordinary priority, which is full enough

    def register(self):
        """Called by a thread on itself, as it starts: it goes to background QoS, and gets the
        override if the app is at full priority."""
        self._set_qos(QOS_CLASS_BACKGROUND)
        ident = threading.get_ident()
        with self._lock:
            self._threads[ident] = self._raise(ident) if self._full else None

    def unregister(self):
        """Called by a thread on itself, as it ends."""
        with self._lock:
            handle = self._threads.pop(threading.get_ident(), None)
        if handle:
            self._lower(handle)

    def refresh(self):
        """Apply what `Load` and `in_use` say now. Cheap when nothing changed."""
        full = self.load.full or bool(self._in_use())
        with self._lock:
            if full == self._full:
                return
            self._full = full
            for ident, handle in self._threads.items():
                if full and handle is None:
                    self._threads[ident] = self._raise(ident)
                elif not full and handle is not None:
                    self._lower(handle)
                    self._threads[ident] = None

    def waited(self, late):
        """The sampler: a timed wait ended `late` seconds after it was due."""
        self._observe(lambda: self.load.waited(self._now(), late))

    def sampled(self):
        """The sampler, after each sample. Reads the CPU only while it has to: at full priority
        because of launch or a busy Mac; while quiet nothing is read."""
        self._observe(self._read_cpu)

    def _read_cpu(self):
        if not self.load.full:
            return
        share = self._idle.share()
        if share is not CpuIdle.NOT_YET:
            self.load.idle(self._now(), share)

    def _observe(self, step):
        before = self.load.state
        step()
        after = self.load.state
        if after != before:
            if after == BUSY:
                log.info("Mac busy: running at full priority")
            elif before == BUSY:
                log.info("Mac quiet again")
            if before == QUIET:
                self._idle.restart()  # the next reading counts from now
        self.refresh()
