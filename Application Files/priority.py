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
  at once, even on a thread that is blocked. A thread takes its override before it lowers its
  own QoS, never after: in between it's at background QoS with nothing to lift it, and while it
  waits there for the interpreter lock it holds up every other thread (#59: a login launch with
  the efficiency cores 0% idle).
- Full priority: while the menu is open or the dashboard is being watched, while a fan profile
  holds the fans, from launch until the app has started and the Mac has been quiet, and while
  it's busy. The main thread only registers once the app has started (`started()`): AppKit's
  own start-up work runs at the main thread's QoS, and handed off at background QoS it can wait
  for the efficiency cores with everything else.
- Holding the fans is a reason of its own, not a late wait to be noticed: the curve's check-ins
  have to reach the fan helper, which hands the fans back to macOS after 10 s without a word,
  and a background thread can wait longer than that on a Mac that has just turned busy.
- Busy: a timed wait of the sampler ended more than `Load.LATE` after it was due. At background
  QoS they normally end 80–100 ms late. On macOS default, with nothing open, a quiet Mac that
  turns busy is exposed until the sampler gets a turn to notice.
- Quiet again: `Load.QUIET_FOR` seconds with `Load.SPARE` (15%) of the efficiency cores' time
  idle. Those are the only cores a background thread runs on, and they can be saturated (Spotlight
  indexing a build's output, say) while the performance cores have time to spare, so the idle
  share of all the cores together says nothing about whether a background thread gets a turn
  (1.2.15 went quiet that way, and the first menu after took 2.3 s). It comes from the
  scheduler's tick counts for each core (`cpu_ticks`), which say nothing about the threads at
  full priority either, and the efficiency cores are the ones the IORegistry's `cpu` nodes give
  `cluster-type` "E", not the first few CPU numbers (`efficiency_cores`). A Mac with none, or
  whose nodes can't be read, counts all of its cores.

`Load` and `efficiency_cores` are the pure parts and are unit-tested.
"""

import ctypes
import ctypes.util
import logging
import threading
import time
from ctypes import POINTER, byref, c_char_p, c_int, c_int64, c_size_t, c_uint, c_uint32, c_void_p

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
    SPARE = 0.15  # share of the efficiency cores' time that must be idle to count as spare CPU: on an M1 Pro, background work was only slow below 5% idle (see CLAUDE.md)
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
        """`share` of the efficiency cores' time was idle since the last reading (None: it couldn't
        be read, which counts as spare: the waits still say when the Mac is busy)."""
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
_libsystem.host_processor_info.argtypes = [c_uint32, c_int, POINTER(c_uint32), POINTER(c_void_p), POINTER(c_uint32)]
_libsystem.vm_deallocate.argtypes = [c_uint32, c_void_p, c_size_t]
_libsystem.mach_host_self.restype = c_uint32
_HOST = _libsystem.mach_host_self()
_TASK = c_uint32.in_dll(_libsystem, "mach_task_self_").value
_PROCESSOR_CPU_LOAD_INFO, _CPU_STATE_IDLE, _CPU_STATES = 2, 2, 4  # <mach/processor_info.h>, <mach/machine.h>

_iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
_cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
_iokit.IORegistryEntryFromPath.restype = c_uint32
_iokit.IORegistryEntryFromPath.argtypes = [c_uint32, c_char_p]
_iokit.IORegistryEntryGetChildIterator.restype = c_int
_iokit.IORegistryEntryGetChildIterator.argtypes = [c_uint32, c_char_p, POINTER(c_uint32)]
_iokit.IOIteratorNext.restype = c_uint32
_iokit.IOIteratorNext.argtypes = [c_uint32]
_iokit.IOObjectRelease.restype = c_int
_iokit.IOObjectRelease.argtypes = [c_uint32]
_iokit.IORegistryEntryCreateCFProperty.restype = c_void_p
_iokit.IORegistryEntryCreateCFProperty.argtypes = [c_uint32, c_void_p, c_void_p, c_uint32]
_cf.CFStringCreateWithCString.restype = c_void_p
_cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
_cf.CFRelease.argtypes = [c_void_p]
_cf.CFGetTypeID.restype = c_size_t
_cf.CFGetTypeID.argtypes = [c_void_p]
_cf.CFDataGetTypeID.restype = c_size_t
_cf.CFNumberGetTypeID.restype = c_size_t
_cf.CFDataGetLength.restype = c_int64
_cf.CFDataGetLength.argtypes = [c_void_p]
_cf.CFDataGetBytePtr.restype = c_void_p
_cf.CFDataGetBytePtr.argtypes = [c_void_p]
_cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
_K_CFSTRING_ENCODING_UTF8, _K_CFNUMBER_SINT64_TYPE = 0x08000100, 4


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
    """Per core, in the order of its CPU number: (user, system, idle, nice) counts of 10 ms
    ticks. None if macOS wouldn't say."""
    count, array, size = c_uint32(), c_void_p(), c_uint32()
    if _libsystem.host_processor_info(_HOST, _PROCESSOR_CPU_LOAD_INFO, byref(count), byref(array), byref(size)) != 0:
        return None
    try:
        numbers = ctypes.cast(array, POINTER(c_uint32))
        return tuple(tuple(numbers[core * _CPU_STATES + state] for state in range(_CPU_STATES))
                     for core in range(count.value))
    finally:
        _libsystem.vm_deallocate(_TASK, array, size.value * 4)  # the array is ours: a page each call if kept


def _registry_value(entry, key):
    """A property of a registry entry as an int (a number) or bytes (data), or None."""
    ref = _iokit.IORegistryEntryCreateCFProperty(entry, key, None, 0)
    if not ref:
        return None
    try:
        kind = _cf.CFGetTypeID(ref)
        if kind == _cf.CFDataGetTypeID():
            return ctypes.string_at(_cf.CFDataGetBytePtr(ref), _cf.CFDataGetLength(ref))
        if kind == _cf.CFNumberGetTypeID():
            number = c_int64()
            _cf.CFNumberGetValue(ref, _K_CFNUMBER_SINT64_TYPE, byref(number))
            return number.value
        return None
    finally:
        _cf.CFRelease(ref)


def cpu_nodes():
    """What the device tree's `cpu` nodes say about each CPU: [(logical-cpu-id, cluster-type)], the
    number `cpu_ticks` counts by and "E" or "P". None when the tree can't be read."""
    cpus = _iokit.IORegistryEntryFromPath(0, b"IODeviceTree:/cpus")
    if not cpus:
        return None
    cluster_type, number = (_cf.CFStringCreateWithCString(None, name, _K_CFSTRING_ENCODING_UTF8)
                            for name in (b"cluster-type", b"logical-cpu-id"))
    nodes, children = [], c_uint32()
    try:
        if _iokit.IORegistryEntryGetChildIterator(cpus, b"IODeviceTree", byref(children)) != 0:
            return None
        try:
            while True:
                node = _iokit.IOIteratorNext(children.value)
                if not node:
                    return nodes
                try:
                    kind, cpu = _registry_value(node, cluster_type), _registry_value(node, number)
                finally:
                    _iokit.IOObjectRelease(node)
                if isinstance(kind, bytes):
                    kind = kind.rstrip(b"\0").decode("ascii", "replace")
                nodes.append((cpu if isinstance(cpu, int) else None, kind if isinstance(kind, str) else None))
        finally:
            _iokit.IOObjectRelease(children.value)
    finally:
        _iokit.IOObjectRelease(cpus)
        _cf.CFRelease(cluster_type)
        _cf.CFRelease(number)


def efficiency_cores(nodes):
    """The CPU numbers of the efficiency cores, from `cpu_nodes()`. () when the Mac has none or
    the nodes don't say clearly (a node with no number or type, or two with one number): then every
    core counts, since a guess about which are the efficiency cores would be worse than none."""
    if not nodes or any(cpu is None or not kind for cpu, kind in nodes) or len({cpu for cpu, _ in nodes}) != len(nodes):
        return ()
    return tuple(sorted(cpu for cpu, kind in nodes if kind == "E"))


class CpuIdle:
    """The share of the efficiency cores' time that was idle since the last reading: the cores the
    threads run on at background QoS (`cores`: their CPU numbers, () for all of them, None to look
    them up at the first reading). The counts are in 10 ms ticks per core, too coarse for a window
    under `MIN_WINDOW` seconds: a reading before that is `NOT_YET`, and the window goes on growing."""

    MIN_WINDOW = 0.9
    NOT_YET = object()

    def __init__(self, read=cpu_ticks, now=time.monotonic, cores=None):
        self._read, self._now = read, now
        self._cores = cores
        self.restart()

    def restart(self):
        """The next reading counts from now."""
        self._last, self._last_at = self._read(), self._now()

    def _look_up(self, count):
        """Which of the `count` CPUs to judge: the efficiency cores, as the registry has them, if
        they are CPUs this reading has; else all of them (an empty tuple)."""
        cores = efficiency_cores(cpu_nodes())
        self._cores = cores if cores and max(cores) < count else ()
        if self._cores:
            log.info("Quiet is judged on the efficiency cores (cpu %s of %d)", ", ".join(map(str, self._cores)), count)
        else:
            log.info("Quiet is judged on all %d cores (no efficiency cores found)", count)

    @property
    def what(self):
        """What the share is a share of, for the log."""
        return "efficiency cores" if self._cores else "cores"

    def share(self):
        at = self._now()
        if at - self._last_at < self.MIN_WINDOW:
            return self.NOT_YET
        ticks, last = self._read(), self._last
        self._last, self._last_at = ticks, at
        if ticks is None or last is None or len(ticks) != len(last):
            return None
        if self._cores is None:
            self._look_up(len(ticks))
        idle = total = 0
        for core in self._cores or range(len(ticks)):
            change = [(a - b) & 0xFFFFFFFF for a, b in zip(ticks[core], last[core])]  # the counts wrap
            idle += change[_CPU_STATE_IDLE]
            total += sum(change)
        return idle / total if total else None


class Priority:
    """Puts the threads that `register()` at full priority or at background QoS, as `Load`,
    `in_use` (the menu or the dashboard) and `holding` (fan control holds the fans) say, once the
    app has `started()`. Safe to call from any thread."""

    def __init__(self, in_use=lambda: False, holding=lambda: False, now=time.monotonic, load=None, idle=None,
                 set_qos=set_own_qos, raise_=raise_thread, lower=lower_thread):
        self.load = load or Load()
        self._in_use = in_use
        self._holding = holding
        self._now = now
        self._idle = idle or CpuIdle(now=now)
        self._set_qos, self._raise, self._lower = set_qos, raise_, lower
        self._seen = ""  # what the last step of `Load` was given, for the log
        self._lock = threading.Lock()
        self._threads = {}  # thread ident -> its override while it's at full priority
        self._full = True  # threads begin at their ordinary priority, which is full enough
        self._starting = True  # until the app has started (`started()`), whatever the Mac is doing

    def register(self):
        """Called by a thread on itself, as it starts: it gets the override if the app is at full
        priority, then goes to background QoS. In that order: the other way round it would sit at
        background QoS with no override for as long as the efficiency cores kept it waiting."""
        ident = threading.get_ident()
        with self._lock:
            self._threads[ident] = self._raise(ident) if self._full else None
        self._set_qos(QOS_CLASS_BACKGROUND)

    def started(self):
        """The app has started (its run loop is running, and the main thread has registered).
        Until now the threads stayed at full priority, so the main thread, still at its own QoS
        while the app started, was never above the others."""
        self._starting = False
        self.refresh()

    def unregister(self):
        """Called by a thread on itself, as it ends."""
        with self._lock:
            handle = self._threads.pop(threading.get_ident(), None)
        if handle:
            self._lower(handle)

    def refresh(self):
        """Apply what `Load`, `in_use` and `holding` say now. Cheap when nothing changed. What they
        say is read under the lock: read before it, a refresh that began just before the menu
        opened could take the lock after the menu's own and lower everything again."""
        with self._lock:
            full = self._starting or self.load.full or bool(self._in_use()) or bool(self._holding())
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
        self._seen = f"a wait ended {late:.1f} s late"
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
            self._seen = "no reading of the cores" if share is None else f"{self._idle.what} {share:.0%} idle"
            self.load.idle(self._now(), share)

    def _observe(self, step):
        """Runs a step of `Load` and logs a switch between busy and quiet, with what the step saw:
        the log is where a Mac that went quiet while it wasn't gets worked out."""
        before = self.load.state
        step()
        after = self.load.state
        if after != before:
            if after == BUSY:
                log.info("Mac busy: running at full priority (%s)", self._seen)
            elif before == BUSY:
                log.info("Mac quiet again (%s)", self._seen)
            if before == QUIET:
                self._idle.restart()  # the next reading counts from now
        self.refresh()
