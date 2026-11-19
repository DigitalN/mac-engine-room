"""Keep Awake: holds the screen on, as Caffeine does, for a while or during work hours.

It holds IOKit's `PreventUserIdleDisplaySleep` assertion: the screen stays on, so the Mac
doesn't idle-sleep, the screen saver doesn't start and the Mac doesn't lock. Closing the lid,
Apple menu › Sleep, locking by hand and macOS's near-empty-battery sleep all still work.

The choices are Off, 1 Hour, 2 Hours and Until Sleep. Every pick counts from now and the last
one wins. The timers run on the wall clock (absolute end times), so sleep neither ends nor
pauses them; Until Sleep ends at the next system sleep. Work hours are one block on chosen
weekdays, in local time. They never wake the Mac: they take effect whenever the Mac is awake
with its screens on during the block. Inside the block, Off is off for the rest of that day's
block, a timer holds until whichever ends later, and Until Sleep carries past the block's end.

It holds only while the screens are awake (not in a Power Nap dark wake) and the screen is
unlocked, and with the battery rule on, not on battery below the chosen level. Letting go while
the Mac is in use declares user activity first, so the screen gets macOS's full display-sleep
time from then instead of dimming and locking at once.

It's evaluated on the sampler's ticks and on notifications only: no timers of its own, so the
app's idle cost doesn't rise. Holding the screen on doesn't raise the app's priority.

Everything above `KeepAwake` is pure and unit-tested.
"""

import ctypes
import ctypes.util
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger("pythonflow")

OFF, HOUR, TWO_HOURS, UNTIL_SLEEP = "off", "1h", "2h", "sleep"
CHOICES = (OFF, HOUR, TWO_HOURS, UNTIL_SLEEP)  # the page's order
CHOICE_NAMES = {OFF: "Off", HOUR: "1 Hour", TWO_HOURS: "2 Hours", UNTIL_SLEEP: "Until Sleep"}
TIMERS = {HOUR: 3600, TWO_HOURS: 7200}
TIMER_WORDS = {HOUR: "1 hour", TWO_HOURS: "2 hours"}
BATTERY_LEVELS = (10, 20, 30, 40, 50)
# Python's weekdays: Monday is 0.
DEFAULT_HOURS = {"on": False, "start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4]}
DEFAULT_BATTERY = {"on": False, "level": 20}
# Seconds a handover from the updater's relaunch stays good: the relaunch waits up to 60 s for the
# old copy to quit, then opens the new one (launch.RELAUNCH_SCRIPT). An older one is from a
# relaunch that never started the new copy, and a later launch by hand brings back nothing.
HANDOVER_FOR = 120.0
HANDOVER_FILE = "keep-awake-handover.json"

# What the winner is: a timer pick, Until Sleep, or the work hours.
TIMER, SLEEP, SCHEDULE = "timer", "sleep", "schedule"
# Why it's paused.
LOCKED, SCREENS, BATTERY = "locked", "screens", "battery"
PAUSE_WORDS = {LOCKED: "the screen is locked", SCREENS: "the screens are asleep"}
# Why it ended, for app.log.
GONE_TO_SLEEP = "the Mac is going to sleep"

_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ── settings ─────────────────────────────────────────────

def minutes(clock):
    """'09:30' → 570, or None if it isn't a valid 24-hour time."""
    m = _CLOCK.match(clock) if isinstance(clock, str) else None
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def validate_hours(raw):
    """Work hours: on/off, start and end as 'HH:MM' (the end after the start: no blocks past
    midnight), and at least one weekday. Whatever is invalid falls back to its default; a start
    and end that don't make a block both do."""
    s = {**DEFAULT_HOURS, "days": list(DEFAULT_HOURS["days"])}
    if not isinstance(raw, dict):
        return s
    if isinstance(raw.get("on"), bool):
        s["on"] = raw["on"]
    start, end = minutes(raw.get("start")), minutes(raw.get("end"))
    if start is not None and end is not None and end > start:
        s["start"], s["end"] = raw["start"], raw["end"]
    days = raw.get("days")
    if isinstance(days, list) and all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days) and days:
        s["days"] = sorted(set(days))
    return s


def validate_battery(raw):
    """The battery rule: on/off and one of BATTERY_LEVELS."""
    s = dict(DEFAULT_BATTERY)
    if not isinstance(raw, dict):
        return s
    if isinstance(raw.get("on"), bool):
        s["on"] = raw["on"]
    if raw.get("level") in BATTERY_LEVELS and not isinstance(raw.get("level"), bool):
        s["level"] = raw["level"]
    return s


def first_weekday(ns_first):
    """NSCalendar's firstWeekday (1 is Sunday … 7 Saturday) as a Python weekday (0 is Monday)."""
    return (int(ns_first) - 2) % 7 if isinstance(ns_first, int) and 1 <= ns_first <= 7 else 0


def week(first):
    """The seven Python weekdays in the order the Mac's calendar starts its week."""
    return [(first + i) % 7 for i in range(7)]


# ── the rules ────────────────────────────────────────────

def block_end(hours, now):
    """When today's work-hours block ends (seconds since the epoch), if `now` is inside it, else
    None. Local time, worked out afresh each time, so a time zone or daylight saving change just
    works."""
    if not hours.get("on"):
        return None
    local = datetime.fromtimestamp(now)
    if local.weekday() not in hours["days"]:
        return None
    start, end = minutes(hours["start"]), minutes(hours["end"])
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    begins = (day + timedelta(minutes=start)).timestamp()
    ends = (day + timedelta(minutes=end)).timestamp()
    return ends if begins <= now < ends else None


def today(now):
    return datetime.fromtimestamp(now).date().isoformat()


class Env:
    """What Keep Awake can't decide itself: whether the screens are awake (not asleep, not a
    dark wake), whether the screen is locked, whether another user's session is showing (`away`,
    treated as locked), and the battery from the latest sample."""

    def __init__(self, screens_awake=True, locked=False, has_battery=False, on_adapter=True, level=None, away=False):
        self.screens_awake, self.locked, self.away = screens_awake, locked, away
        self.has_battery, self.on_adapter, self.level = has_battery, on_adapter, level

    def battery_low(self, rule):
        """The battery rule applies: on battery, below its level. On the adapter it holds even
        below the level."""
        return bool(rule.get("on") and self.has_battery and not self.on_adapter
                    and isinstance(self.level, (int, float)) and self.level < rule["level"])

    @property
    def in_use(self):
        return self.screens_awake and not (self.locked or self.away)


class Status:
    """What Keep Awake is doing: `kind` and `until` of whatever holds the screen on (None when
    nothing wants to), `paused` and `holding`, the choice to light up and the battery level for
    the line."""

    def __init__(self, kind=None, until=None, pick=None, paused=None, off_today=False, battery_low=False,
                 level=None, pick_until=None):
        self.kind, self.until, self.pick, self.paused = kind, until, pick, paused
        self.off_today, self.battery_low, self.level, self.pick_until = off_today, battery_low, level, pick_until

    @property
    def holding(self):
        return self.kind is not None and self.paused is None

    @property
    def lit(self):
        """The choice to light up: the pick while it lasts, Off when nothing holds the screen on
        (or it's off for the rest of today's work hours), none while work hours hold it."""
        if self.pick:
            return self.pick
        return None if self.kind == SCHEDULE else OFF


def clock_24(t):
    """For app.log: '17:00'."""
    return datetime.fromtimestamp(t).strftime("%H:%M")


def duration_words(seconds):
    """'42 more minutes', '1 more hour', '1 more hour and 12 minutes': rounded up to the minute,
    so it never says 0."""
    total = max(1, math.ceil(seconds / 60 - 1e-9))
    hours, mins = divmod(total, 60)
    if not hours:
        return f"{mins} more minute{'s' if mins != 1 else ''}"
    text = f"{hours} more hour{'s' if hours != 1 else ''}"
    return text + (f" and {mins} minute{'s' if mins != 1 else ''}" if mins else "")


def sentence(status, now, format_time=clock_24):
    """The line under the choices, in the menu and on the page, or None. `format_time` follows
    the Mac's 12/24-hour setting (KeepAwake gives it NSDateFormatter's short time style)."""
    if status.kind is None:
        return "Off for the rest of today's work hours." if status.off_today else None
    if status.battery_low:
        return f"Paused because the battery is below {status.level}%."
    if status.kind == SLEEP:
        return "Screen stays on until the Mac sleeps."
    if status.kind == TIMER:
        return f"Screen stays on for {duration_words(status.until - now)}."
    return f"Screen stays on until {format_time(status.until)} (work hours)."


class Model:
    """Keep Awake's rules. `choose`, `slept` and `evaluate` change it; `evaluate` returns the
    Status and the lines for app.log, and says when letting go should declare user activity."""

    def __init__(self):
        self.pick = None  # HOUR, TWO_HOURS or UNTIL_SLEEP while one lasts
        self.until = None  # a timer's end, seconds since the epoch
        self.off_day = None  # the day ('2026-11-19') whose work hours Off was chosen in
        self._why = None  # why it ended, when an event (Off, sleep) ended it
        self._picked = False  # a pick since the last evaluate, for its line in app.log
        self._last = Status()

    def choose(self, choice, now, hours):
        """Every pick counts from now; the last one wins."""
        if choice not in CHOICES:
            return
        if choice == OFF:
            self._why = "Off chosen"
            self.pick = self.until = None
            # Inside the block, Off is off for the rest of it, back by itself the next workday.
            self.off_day = today(now) if block_end(hours, now) is not None else None
            return
        self.pick, self.off_day, self._picked = choice, None, True
        self.until = now + TIMERS[choice] if choice in TIMERS else None

    def slept(self):
        """The Mac is going to sleep (however it was caused): Until Sleep is over. Timers and
        work hours carry on by the clock."""
        if self.pick == UNTIL_SLEEP:
            self.pick = None
            self._why = GONE_TO_SLEEP

    def hand_over(self, now):
        """What the updater's relaunch passes to the new copy: the pick and today's Off, with the
        same end times."""
        return {"written": now, "pick": self.pick, "until": self.until, "off_day": self.off_day}

    def take_over(self, data, now):
        """Picks up a handover from the copy that relaunched into this one. Returns whether it
        did: only a fresh one counts (HANDOVER_FOR)."""
        if not isinstance(data, dict) or not isinstance(data.get("written"), (int, float)):
            return False
        if not 0 <= now - data["written"] <= HANDOVER_FOR:
            return False
        pick, until, off_day = data.get("pick"), data.get("until"), data.get("off_day")
        if pick in TIMERS and isinstance(until, (int, float)):
            self.pick, self.until = pick, float(until)
        elif pick == UNTIL_SLEEP:
            self.pick, self.until = pick, None
        if isinstance(off_day, str):
            self.off_day = off_day
        return True

    def evaluate(self, now, hours, battery_rule, env):
        """Returns (status, notes, declare): notes are the lines for app.log, and `declare` says
        to declare user activity as it lets go (the Mac is in use and it wasn't sleep)."""
        notes = []
        why = self._why
        self._why = None
        if self.pick in TIMERS and now >= self.until:
            why = why or f"{TIMER_WORDS[self.pick]} {'is' if self.pick == HOUR else 'are'} up"
            self.pick = self.until = None
        ends = block_end(hours, now)
        if self.off_day is not None and (ends is None or self.off_day != today(now)):
            self.off_day = None  # that day's block is over
        schedule = ends is not None and self.off_day is None

        kind = until = None
        if self.pick == UNTIL_SLEEP:
            kind = SLEEP
        elif self.pick in TIMERS and not (schedule and ends >= self.until):
            kind, until = TIMER, self.until  # whichever ends later wins
        elif schedule:
            kind, until = SCHEDULE, ends

        low = env.battery_low(battery_rule)
        paused = None
        if kind is not None:
            paused = LOCKED if env.locked or env.away else SCREENS if not env.screens_awake else BATTERY if low else None
        status = Status(kind, until, self.pick, paused, off_today=self.off_day is not None and ends is not None,
                        battery_low=low and kind is not None, level=battery_rule.get("level"), pick_until=self.until)

        last = self._last
        if kind is not None and (self._picked or (kind, until) != (last.kind, last.until)):
            notes.append(self._on_note(status, ends))
        elif kind is None and last.kind is not None:
            if why is None and last.kind == SCHEDULE:
                why = "work hours turned off" if not hours.get("on") else (
                    "work hours are over" if now >= last.until else "work hours changed")
            if status.off_today:
                why = f"{why}, for the rest of today's work hours"
            notes.append(f"Keep Awake off ({why or 'nothing holds the screen on'})")
        if kind is not None:
            if paused and (paused != last.paused or last.kind is None):
                notes.append(f"Keep Awake paused ({self._pause_words(paused, battery_rule)})")
            elif not paused and last.paused and last.kind is not None:
                notes.append("Keep Awake resumed")
        declare = last.holding and not status.holding and env.in_use and why != GONE_TO_SLEEP
        self._picked = False
        self._last = status
        return status, notes, declare

    def _on_note(self, status, ends):
        if status.kind == SLEEP:
            return "Keep Awake on until the Mac sleeps"
        if status.kind == TIMER:
            return f"Keep Awake on for {TIMER_WORDS[self.pick]} (until {clock_24(status.until)})"
        note = f"Keep Awake on until {clock_24(status.until)} (work hours)"
        if self._picked and self.pick in TIMERS:  # a timer picked inside the block, which ends later
            note += f"; {TIMER_WORDS[self.pick]} chosen, until {clock_24(self.until)}"
        return note

    @staticmethod
    def _pause_words(paused, rule):
        return f"the battery is below {rule.get('level')}%" if paused == BATTERY else PAUSE_WORDS[paused]


# ── the assertion (IOKit) ────────────────────────────────

_K_CFSTRING_ENCODING_UTF8 = 0x08000100
_K_IOPM_ASSERTION_LEVEL_ON = 255
_K_IOPM_USER_ACTIVE_LOCAL = 0
ASSERTION_TYPE = b"PreventUserIdleDisplaySleep"  # kIOPMAssertionTypePreventUserIdleDisplaySleep
ASSERTION_NAME = b"Mac Engine Room Keep Awake"


class _IOKit:
    """The three IOKit power calls, through ctypes like the rest of the app's IOKit use."""

    def __init__(self):
        vp, u32 = ctypes.c_void_p, ctypes.c_uint32
        self._cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        self._cf.CFStringCreateWithCString.restype = vp
        self._cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, u32]
        self._io = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        self._io.IOPMAssertionCreateWithName.argtypes = [vp, u32, vp, ctypes.POINTER(u32)]
        self._io.IOPMAssertionCreateWithName.restype = ctypes.c_int
        self._io.IOPMAssertionRelease.argtypes = [u32]
        self._io.IOPMAssertionRelease.restype = ctypes.c_int
        self._io.IOPMAssertionDeclareUserActivity.argtypes = [vp, ctypes.c_int, ctypes.POINTER(u32)]
        self._io.IOPMAssertionDeclareUserActivity.restype = ctypes.c_int
        # Created once and kept for the life of the process.
        self._type = self._cf.CFStringCreateWithCString(None, ASSERTION_TYPE, _K_CFSTRING_ENCODING_UTF8)
        self._name = self._cf.CFStringCreateWithCString(None, ASSERTION_NAME, _K_CFSTRING_ENCODING_UTF8)
        self._activity = ctypes.c_uint32(0)  # reused for each declaration, as IOKit allows

    def hold(self):
        """The assertion's id, or None if IOKit refused."""
        aid = ctypes.c_uint32(0)
        result = self._io.IOPMAssertionCreateWithName(self._type, _K_IOPM_ASSERTION_LEVEL_ON, self._name, ctypes.byref(aid))
        if result != 0:
            log.warning("Keep Awake couldn't hold the screen on (IOKit error 0x%x)", result & 0xFFFFFFFF)
            return None
        return aid.value

    def release(self, aid):
        self._io.IOPMAssertionRelease(aid)

    def declare_activity(self):
        result = self._io.IOPMAssertionDeclareUserActivity(self._name, _K_IOPM_USER_ACTIVE_LOCAL, ctypes.byref(self._activity))
        if result != 0:
            log.warning("Keep Awake couldn't declare user activity (IOKit error 0x%x)", result & 0xFFFFFFFF)


# ── the controller ───────────────────────────────────────

def mac_time_formatter():
    """Times in the Mac's own style (NSDateFormatter's short time style follows the 12/24-hour
    setting): '5:00 PM' or '17:00'."""
    from Foundation import NSDate, NSDateFormatter, NSDateFormatterNoStyle, NSDateFormatterShortStyle, NSLocale

    formatter = NSDateFormatter.alloc().init()
    formatter.setLocale_(NSLocale.autoupdatingCurrentLocale())
    formatter.setDateStyle_(NSDateFormatterNoStyle)
    formatter.setTimeStyle_(NSDateFormatterShortStyle)
    lock = threading.Lock()  # one formatter, used from the main thread and the page's

    def format_time(t):
        with lock:
            return str(formatter.stringFromDate_(NSDate.dateWithTimeIntervalSince1970_(t)))

    return format_time


class KeepAwake:
    """Runs the Model: on each sample (`feed`), on picks (`choose`), on settings changes and on
    the notifications `start` observes. Holds or releases the assertion, logs, and calls
    `on_change` (any thread) when whether it holds the screen on changes."""

    def __init__(self, settings, data_dir, on_change=lambda holding: None, iokit=None, clock=time.time,
                 format_time=None):
        self._settings = settings
        self._handover_path = os.path.join(str(data_dir), HANDOVER_FILE)
        self._on_change = on_change
        self._iokit = iokit
        self._clock = clock
        self._format_time = format_time
        self._model = Model()
        self._env = Env()
        self._lock = threading.RLock()
        self._assertion = None
        self._status = Status()
        self._observers = []

    @property
    def holding(self):
        return self._status.holding

    def start(self):
        """Main thread, at launch: picks up a handover from the updater's relaunch, reads whether
        the screens are awake and the screen is locked, and observes their changes and sleep."""
        if self._iokit is None:
            self._iokit = _IOKit()
        if self._format_time is None:
            self._format_time = mac_time_formatter()
        self._take_over()
        self._env.screens_awake, (self._env.locked, self._env.away) = _screens_awake(), _locked()
        self._observe()
        self.evaluate()

    def _take_over(self):
        """The handover is consumed once: read and deleted, so a quit by the user, a restart or a
        logout brings back nothing but the work hours."""
        try:
            with open(self._handover_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        finally:
            try:
                os.remove(self._handover_path)
            except OSError:
                pass
        with self._lock:
            if self._model.take_over(data, self._clock()):
                log.info("Keep Awake carried over from the copy that updated itself")

    def _observe(self):
        import AppKit
        from Foundation import NSDistributedNotificationCenter

        workspace = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
        distributed = NSDistributedNotificationCenter.defaultCenter()
        for center, name, change in (
                (workspace, AppKit.NSWorkspaceScreensDidSleepNotification, {"screens_awake": False}),
                (workspace, AppKit.NSWorkspaceScreensDidWakeNotification, {"screens_awake": True}),
                (workspace, AppKit.NSWorkspaceSessionDidResignActiveNotification, {"away": True}),
                (workspace, AppKit.NSWorkspaceSessionDidBecomeActiveNotification, {"away": False}),
                (distributed, "com.apple.screenIsLocked", {"locked": True}),
                (distributed, "com.apple.screenIsUnlocked", {"locked": False})):
            self._observers.append((center, center.addObserverForName_object_queue_usingBlock_(
                name, None, None, lambda notification, change=change: self.environment(**change))))
        self._observers.append((workspace, workspace.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceWillSleepNotification, None, None, lambda notification: self.will_sleep())))
        self._observers.append((workspace, workspace.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidWakeNotification, None, None, lambda notification: self.will_sleep(woke=True))))

    def environment(self, **change):
        with self._lock:
            for key, value in change.items():
                setattr(self._env, key, value)
            self.evaluate()

    def will_sleep(self, woke=False):
        """Until Sleep ends at the next sleep. A wake ends one picked before the sleep too, in case
        the sleep came without its notification."""
        with self._lock:
            self._model.slept()
            self.evaluate()

    def feed(self, sample):
        """Sampler thread: each sample's battery, and the clock."""
        if not sample:
            return
        with self._lock:
            self._env.has_battery = bool(sample.get("has_battery"))
            self._env.on_adapter = bool(sample.get("external_connected"))
            self._env.level = sample.get("battery_level")
            self.evaluate()

    def choose(self, choice):
        """A pick from the menu or the page. Returns the status for showing at once."""
        with self._lock:
            self._model.choose(choice, self._clock(), self._hours())
            self.evaluate()
            return self.status()

    def settings_changed(self, values):
        self.evaluate()

    def _hours(self):
        return self._settings.get()["keep_awake_hours"]

    def evaluate(self):
        with self._lock:
            values = self._settings.get()
            was = self._status.holding
            status, notes, declare = self._model.evaluate(
                self._clock(), values["keep_awake_hours"], values["keep_awake_battery"], self._env)
            self._status = status
            for note in notes:
                log.info(note)
            if self._iokit is not None:
                if status.holding and self._assertion is None:
                    self._assertion = self._iokit.hold()
                elif not status.holding and self._assertion is not None:
                    if declare:
                        self._iokit.declare_activity()  # before letting go, so the screen doesn't go dark at once
                    self._iokit.release(self._assertion)
                    self._assertion = None
        if status.holding != was:
            self._on_change(status.holding)

    def status(self):
        """For the menu and the page: what's lit, the line, and whether it holds the screen on."""
        with self._lock:
            s, now = self._status, self._clock()
            return {"choice": s.lit, "holding": s.holding,
                    "line": sentence(s, now, self._format_time or clock_24)}

    def hand_over(self):
        """The updater is relaunching into a new version: the pick and today's Off go to the new
        copy with the same end times, and user activity is declared so the screen gets its full
        display-sleep time over the gap. Main thread."""
        with self._lock:
            data = self._model.hand_over(self._clock())
            try:
                tmp = self._handover_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self._handover_path)
            except OSError as e:
                log.warning("Couldn't hand Keep Awake over to the new version: %s", e)
            if self._assertion is not None and self._iokit is not None:
                self._iokit.declare_activity()


def _screens_awake():
    """Whether the main display is awake, at launch (afterwards, the notifications say)."""
    try:
        import Quartz

        return not Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID())
    except Exception:
        return True


def _locked():
    """(locked, away) at launch: the screen is locked, another user's session is showing."""
    try:
        import Quartz

        session = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("CGSSessionScreenIsLocked")), not session.get("kCGSSessionOnConsoleKey", True)
    except Exception:
        return False, False
