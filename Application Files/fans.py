"""Fan control for the Thermals tool: the profiles, their curves, the safety override, and
the root helper that writes the speeds.

The curve runs here, once a second, on the hottest CPU or GPU *core*: not the chip hotspots
or the battery sensors ThermalForge mixed in, so the same numbers speed the fans up later and
quieter. A chosen profile has the fans from the moment it's chosen: Balanced and Performance
hold them at their minimum speed until the cores reach the profile's start, Max at full speed
the whole time. ThermalForge, and this app until 1.2.10, left them with macOS until then, so
choosing a profile on a cool Mac seemed to do nothing. Writing a fan speed needs root, which only the small Swift helper in "Fan Helper"
has. launchd starts it on demand, it only answers this app, and its safety nets (the fans go
back to macOS if this app disconnects, stops checking in, or the Mac sleeps) work even if
this app hangs.

The profiles and their numbers come from ThermalForge (github.com/ProducerGuy/ThermalForge,
MIT; see THIRD-PARTY-NOTICES), with fixes from its issues: start at the fans' minimum (#37),
and a safety override that follows macOS's thermal pressure rather than a fixed 95° (#68).

Everything above FanControl is pure and unit-tested.
"""

import errno
import logging
import math
import socket
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("pythonflow")


class Profile:
    """A fan curve. Temperatures are °C; `top` and the ramps are fractions of each fan's
    maximum speed; `ramp_up` and `ramp_down` are per second. Below `start` the fans hold their
    minimum speed; from there they speed up along `shape` to `top` at `full`."""

    def __init__(self, name, start, full, top, shape, ramp_up, ramp_down, instant=False):
        self.name, self.start, self.full, self.top = name, start, full, top
        self.shape, self.ramp_up, self.ramp_down = shape, ramp_up, ramp_down
        self.instant = instant  # Max: top speed the whole time, no curve

    def tuned(self, tuning):
        """A copy with the user's Speeds up at, Top speed at and Top speed (already validated)."""
        copy = Profile(self.name, self.start, self.full, self.top, self.shape, self.ramp_up,
                       self.ramp_down, self.instant)
        copy.start = tuning.get("start", copy.start)
        copy.full = tuning.get("full", copy.full)
        copy.top = tuning.get("top", round(copy.top * 100)) / 100
        return copy


# ThermalForge's numbers: where the fans speed up from their minimum, where they reach top
# speed, top speed, the curve's shape, and the ramps up and down.
PROFILES = {
    "balanced": Profile("Balanced", 55, 70, 0.60, "ease_in", 0.05, 0.025),
    "performance": Profile("Performance", 55, 65, 0.85, "linear", 0.10, 0.04),
    "max": Profile("Max", None, None, 1.00, "linear", 1.0, 0.025, instant=True),
}
MACOS = "macos"
PROFILE_IDS = (MACOS, "balanced", "performance", "max")
PROFILE_NAMES = {MACOS: "macOS default", **{k: p.name for k, p in PROFILES.items()}}
MENU_PROFILES = [MACOS, "balanced", "max"]  # the three the menu shows until others are chosen in Settings
TUNABLE = ("balanced", "performance")  # Max has nothing to tune

# Settings > Fan profiles limits: Speeds up at 50–85°, Top speed at at least 5° above it and at
# most 94°, Top speed 30–100%.
START_RANGE = (50.0, 85.0)
FULL_MAX = 94.0
MIN_SPAN = 5.0
TOP_RANGE = (30, 100)
TUNING_LIMITS = {"start": START_RANGE, "full_max": FULL_MAX, "min_span": MIN_SPAN, "top": TOP_RANGE}

def validate_tuning(raw):
    """Clean {profile: {"start", "full", "top"}} from settings.json: unknown profiles and keys
    dropped, numbers clamped to the limits above. Temperatures keep one decimal, so a value
    typed in Fahrenheit comes back as typed."""
    tuning = {}
    if not isinstance(raw, dict):
        return tuning
    for pid, values in raw.items():
        if pid not in TUNABLE or not isinstance(values, dict):
            continue

        def number(key):
            v = values.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None

        clean = {}
        start, full, top = number("start"), number("full"), number("top")
        if start is not None:
            clean["start"] = round(min(max(start, START_RANGE[0]), START_RANGE[1]), 1)
        lowest = clean.get("start", PROFILES[pid].start) + MIN_SPAN
        if full is not None:
            clean["full"] = round(min(max(full, lowest), FULL_MAX), 1)
        elif "start" in clean and PROFILES[pid].full < lowest:
            clean["full"] = lowest  # a later Speeds up at pushes the default Top speed at along
        if top is not None:
            clean["top"] = int(min(max(round(top), TOP_RANGE[0]), TOP_RANGE[1]))
        if clean:
            tuning[pid] = clean
    return tuning


def page_profiles():
    """The profiles' names and defaults, for the page; Settings tunes the `tunable` ones."""
    return [{"id": pid, "name": p.name, "start": p.start, "full": p.full, "top": round(p.top * 100),
             "tunable": pid in TUNABLE} for pid, p in PROFILES.items()]


def profile(pid, tuning=None):
    """The profile with the user's tuning, or None for macOS default."""
    base = PROFILES.get(pid)
    return base.tuned((tuning or {}).get(pid, {})) if base else None


def shaped(position, shape):
    return position * position if shape == "ease_in" else position


class Curve:
    """A profile's curve, ticked about once a second with the hottest core. Returns the speed
    to hold as a fraction of each fan's maximum, from the first tick: never below the fans'
    minimum, so they're the profile's whatever the temperature."""

    def __init__(self, profile):
        self.profile = profile
        self.fraction = None  # what the last tick asked for; None before the first
        self._last = None

    def reset(self):
        """Start over, e.g. after sleep: the next speed comes from the temperatures then,
        never an old one (ThermalForge #48)."""
        self.fraction = self._last = None

    def tick(self, now, temp, min_fraction):
        p = self.profile
        dt = min(max(now - self._last, 0.0), 3.0) if self._last is not None else 1.0
        self._last = now
        top = max(p.top, min_fraction)
        if p.instant:
            self.fraction = top
            return top
        if self.fraction is None:
            self.fraction = min_fraction  # from the fans' minimum, then up the curve (ThermalForge #37)
        if temp is None:
            return self.fraction  # no reading: keep what's there
        if temp < p.start:
            target = min_fraction
        elif temp >= p.full:
            target = p.top
        else:
            target = shaped((temp - p.start) / (p.full - p.start), p.shape) * p.top
        target = min(max(target, min_fraction), top)

        if target > self.fraction:
            target = min(target, self.fraction + p.ramp_up * dt)
        elif target < self.fraction:
            target = max(target, self.fraction - p.ramp_down * dt)
        self.fraction = target
        return target


PRESSURE_NAMES = ("nominal", "fair", "serious", "critical")  # NSProcessInfoThermalState


class Safety:
    """Full speed while a profile holds the fans and the Mac is too hot. Not a fixed
    95°: on M4 the hotspots read 96–113° while macOS is calm (ThermalForge #68). It follows
    macOS's thermal pressure (Serious or Critical at once, Fair after 30 seconds), backed up per
    kind of sensor. Hotspots are judged by their second-highest reading, so one bad sensor can't
    trip it or keep it on. It clears once every reading is 5° below where it trips."""

    # (reading, trips at, after seconds)
    LIMITS = {"hotspot": (118.0, 10.0), "chassis": (95.0, 30.0), "battery": (50.0, 60.0)}
    FAIR_AFTER = 30.0
    CLEAR_BELOW = 5.0

    def __init__(self):
        self.reason = None  # what tripped it, while it's on
        self._since = {}

    def reset(self):
        self.reason = None
        self._since.clear()

    def tick(self, now, readings):
        """`readings`: {"pressure": 0–3, "hotspot", "chassis", "battery": °C or None}."""
        pressure = readings.get("pressure") or 0
        over = {}
        if pressure >= 2:
            over["pressure"] = 0.0
        elif pressure == 1:
            over["pressure"] = self.FAIR_AFTER
        for key, (limit, after) in self.LIMITS.items():
            value = readings.get(key)
            if isinstance(value, (int, float)) and value >= limit:
                over[key] = after
        for key in list(self._since):
            if key not in over:
                del self._since[key]
        tripped = None
        for key, after in over.items():
            since = self._since.setdefault(key, now)
            if tripped is None and now - since >= after:
                tripped = key
        if tripped and self.reason is None:
            self.reason = "pressure" if tripped == "pressure" else tripped
            return self.reason
        if self.reason is not None and pressure == 0 and all(
                not isinstance(readings.get(k), (int, float)) or readings[k] < limit - self.CLEAR_BELOW
                for k, (limit, _) in self.LIMITS.items()):
            self.reason = None
        return self.reason

    @staticmethod
    def describe(reason, pressure=0):
        if reason == "pressure":
            return f"macOS reports {PRESSURE_NAMES[max(pressure, 1)]} thermal pressure"
        return {"hotspot": "part of the chip passed 118°", "chassis": "a sensor inside the Mac passed 95°",
                "battery": "the battery passed 50°"}.get(reason, reason)


def speeds(fraction, limits):
    """Each fan's rpm for a fraction of its maximum, never below its minimum."""
    return [int(round(min(max(hi * fraction, lo), hi))) for lo, hi in limits]


def min_fraction(limits):
    """The fraction of maximum that's the slowest fan's minimum: speeds() keeps each fan at
    least at its own, so at this fraction every fan is at its minimum."""
    return min((lo / hi for lo, hi in limits if hi), default=0.0)


class HelperError(Exception):
    """The helper's "error <reason>", or a failure to reach it ("unreachable")."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# SMAppServiceStatus
NOT_REGISTERED, ENABLED, REQUIRES_APPROVAL, NOT_FOUND = 0, 1, 2, 3
SO_NOSIGPIPE = 0x1022


class Helper:
    """The fan helper: its launchd job (registered with SMAppService, which the user approves
    once in System Settings) and a connection to its socket."""

    def __init__(self, app_path, bundle_id):
        self._service = None
        self._sock = None
        self._app_path = app_path
        self._plist = f"{bundle_id}.fans.plist"
        self._reason = None
        self.socket_path = f"/var/run/{bundle_id}.fans.sock"

    @property
    def unavailable_reason(self):
        """Why fan control can't work in this copy, or None: the end of "Fan control isn't
        available …". Worked out the first time a profile is chosen, so a Mac left on macOS
        default never pays for it."""
        if self._reason is None:
            self._reason = self._why_unavailable(self._app_path, self._plist) or ""
        return self._reason or None

    def _why_unavailable(self, app_path, plist):
        import platform

        import launch
        import power
        import updater

        if app_path is None:
            return "when Mac Engine Room runs from source"
        if not power._is_apple_silicon():
            return "on a Mac with an Intel processor"
        if int((platform.mac_ver()[0] or "0").split(".")[0]) < 13:
            return "before macOS 13"
        if not launch.in_applications_folder(app_path):
            return "until Mac Engine Room is in your Applications folder"
        if not (Path(app_path) / "Contents" / "Library" / "LaunchDaemons" / plist).exists():
            return "in this copy, which was built without the fan helper"
        if updater.own_team(app_path) is None:
            return "in this copy, which isn't signed by a developer team"
        try:
            import objc

            objc.loadBundle("ServiceManagement", {},
                            bundle_path="/System/Library/Frameworks/ServiceManagement.framework")
            service_class = objc.lookUpClass("SMAppService")
            for selector in (b"registerAndReturnError:", b"unregisterAndReturnError:"):
                objc.registerMetaDataForSelector(b"SMAppService", selector,
                                                 {"arguments": {2: {"type_modifier": b"o"}}})
            self._service = service_class.daemonServiceWithPlistName_(plist)
        except Exception as e:
            log.exception("Fan control isn't available")
            return f"because macOS didn't offer the background service it needs ({e})"
        return None

    def status(self):
        return self._service.status() if self._service is not None else NOT_FOUND

    def register(self):
        """Asks macOS to allow the helper. The first time, macOS reports that the user has to
        approve it, as documented; the status is then REQUIRES_APPROVAL."""
        ok, error = self._service.registerAndReturnError_(None)
        if not ok and self.status() not in (REQUIRES_APPROVAL, ENABLED):
            raise HelperError(f"because macOS refused the fan helper ({error.localizedDescription() if error else 'no reason given'})")

    def open_login_items(self):
        import objc

        objc.lookUpClass("SMAppService").openSystemSettingsLoginItems()

    def request(self, line, timeout=5.0):
        """Sends one command and returns the reply after "ok". Connects as needed; the
        connection stays open while the fans are held, since the helper hands them back
        when it closes."""
        for attempt in (1, 2):  # once more on a fresh connection if the helper had quit
            try:
                if self._sock is None:
                    self._sock = self._connect()
                self._sock.settimeout(timeout)
                self._sock.sendall(line.encode() + b"\n")
                reply = self._read_line()
                break
            except OSError as e:
                self.close()
                if attempt == 2 or isinstance(e, socket.timeout):
                    raise HelperError("unreachable") from e
        if reply.startswith("ok"):
            return reply[2:].strip()
        raise HelperError(reply.removeprefix("error").strip() or "no reply")

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, SO_NOSIGPIPE, 1)  # a dropped connection mustn't kill the app (ThermalForge #71)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self._buffer = b""
        return sock

    def _read_line(self):
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionResetError(errno.ECONNRESET, "the helper closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("utf-8", "replace")

    def close(self):
        if self._sock is not None:
            self._sock.close()
            self._sock = None


def thermal_pressure():
    """macOS's thermal pressure, 0 (nominal) to 3 (critical)."""
    from Foundation import NSProcessInfo

    return int(NSProcessInfo.processInfo().thermalState())


class FanControl(threading.Thread):
    """Runs the chosen profile on its own thread, once per sample (once a second while a
    profile is chosen). A request to the helper can take seconds (the first unlock), so it
    never holds up sampling.

    `status` is for the page and the menu: {"profile", "state", …} where state is "off"
    (macOS default), "nofans", "unavailable" (with a "reason"), "approval" (waiting for the
    user to allow the helper), "holding" (at "targets" rpm), "override" (full speed,
    "reason"), "other" (another app has the fans) or "error" ("reason"). An "unavailable" reason finishes "Fan control isn't available …", an
    "error" one "Fan control stopped because …". An override's reason is Safety's ("pressure", "hotspot", "chassis" or
    "battery"), with "pressure" 0–3, so the page can put temperatures in the user's unit."""

    # Seconds between asking macOS whether the user has allowed the helper. Once allowed, it's
    # only asked again when the helper can't be reached (turned off in Login Items): each
    # answer comes from backgroundtaskmanagementd, which can be busy for seconds.
    APPROVAL_CHECK = 3.0
    RETRY_AFTER = 30.0  # after the helper failed
    OTHER_APP_RETRY = 10.0

    def __init__(self, settings, helper, fan_limits, priority=None):
        super().__init__(name="pythonflow-fans", daemon=True)
        self._settings = settings
        self._helper = helper
        self._fan_limits = fan_limits
        self._priority = priority  # priority.Priority: this thread runs at the app's one priority
        self._wake = threading.Event()
        self._latest = (None, None)
        self._woke_from_sleep = False
        self._stopping = False
        self._pid = None
        self._curve = None
        self._safety = Safety()
        self._sent = None  # the speeds last set while holding
        self._held = False
        self._registered = False
        self._service = None
        self._next_check = 0.0
        self._retry_at = 0.0
        self._last_state = None
        self.status = {"profile": MACOS, "state": "off"}

    @property
    def holding(self):
        return self._held

    @property
    def running(self):
        """A profile is chosen and in charge, so the sampler runs once a second."""
        return self.status["state"] in ("holding", "override", "other")

    def feed(self, thermals, power):
        """The sampler's latest readings (thermals may be None if the SMC couldn't be read).
        Each one is a tick, unless macOS default is chosen and there's nothing to do. Only
        samples tick: if sampling stopped, so would the check-ins, and the helper would hand
        the fans back rather than hold a speed for temperatures nobody reads."""
        self._latest = (thermals, power)
        if self._held or self.status["state"] != "off":
            self._wake.set()

    def nudge(self):
        """Something changed (a setting): tick now."""
        self._wake.set()

    def did_wake(self):
        """The helper handed the fans back before the Mac slept; start again from the
        temperatures now."""
        self._woke_from_sleep = True
        self._wake.set()

    def open_login_items(self):
        self._helper.open_login_items()

    def stop(self):
        """At quit: lets a write in progress finish, then hands the fans back (ThermalForge #76, #77)."""
        self._stopping = True
        self._wake.set()
        if self.is_alive():
            self.join(timeout=45)
        self._release("Mac Engine Room quit")
        self._helper.close()

    def run(self):
        import objc

        if self._priority:
            self._priority.register()
        try:
            self._wake.set()  # the profile chosen last time
            while not self._stopping:
                self._wake.wait()
                self._wake.clear()
                if self._stopping:
                    break
                with objc.autorelease_pool():
                    try:
                        self._tick(time.monotonic())
                    except Exception:
                        log.exception("Fan control failed")
        finally:
            if self._priority:
                self._priority.unregister()

    # ── one tick ──

    def _tick(self, now):
        settings = self._settings.get()
        pid = settings["fan_profile"]
        if self._woke_from_sleep:
            self._woke_from_sleep = False
            self._lost()
        if pid != self._pid:
            self._switch(pid)
        if pid == MACOS:
            self._release("macOS default chosen")
            return self._set_status("off")
        limits = self._fan_limits()
        if not limits:
            return self._set_status("nofans")
        if self._helper.unavailable_reason:
            return self._set_status("unavailable", reason=self._helper.unavailable_reason)
        if not self._allowed(now) or now < self._retry_at:
            return

        thermals, power = self._latest
        temps = [thermals.get(k) for k in ("cpu", "gpu")] if thermals else []
        temp = max((t for t in temps if t is not None), default=None)
        self._curve.profile = profile(pid, settings["fan_tuning"])
        targets = speeds(self._curve.tick(now, temp, min_fraction(limits)), limits)

        pressure = thermal_pressure()
        reason = self._safety.tick(now, {"pressure": pressure, "hotspot": (thermals or {}).get("hotspot"),
                                         "chassis": (thermals or {}).get("chassis"),
                                         "battery": (power or {}).get("temperature")})
        if reason:
            targets = [hi for _, hi in limits]
        if self._hold(targets, temp, now):
            if reason:
                self._set_status("override", reason=reason, pressure=pressure, targets=targets)
            else:
                self._set_status("holding", targets=targets)

    def _switch(self, pid):
        log.info("Fan profile: %s", PROFILE_NAMES.get(pid, pid))
        self._pid = pid
        self._safety.reset()
        self._curve = Curve(PROFILES.get(pid)) if pid in PROFILES else None
        if self._curve is not None and self._held and self._sent:
            # Carry on from the speed the fans have now rather than handing them back.
            limits = self._fan_limits()
            self._curve.fraction = max((rpm / hi for rpm, (_, hi) in zip(self._sent, limits) if hi), default=None)
        self._retry_at = 0.0

    def _allowed(self, now):
        """The helper is registered and the user has allowed it. It's registered the first
        time a profile is chosen, and never before."""
        if now >= self._next_check:
            self._service = self._helper.status()
            self._next_check = math.inf if self._service == ENABLED else now + self.APPROVAL_CHECK
            # A daemon that was never registered reports NOT_FOUND, not NOT_REGISTERED (measured).
            if self._service in (NOT_REGISTERED, NOT_FOUND) and not self._registered:
                self._registered = True
                try:
                    self._helper.register()
                except HelperError as e:
                    self._set_status("unavailable", reason=str(e))
                    self._next_check = now + self.RETRY_AFTER
                    return False
                self._service = self._helper.status()
                log.info("Asked macOS to allow the fan helper (status %d)", self._service)
        if self._service == ENABLED:
            return True
        self._lost()
        if self._service in (REQUIRES_APPROVAL, NOT_REGISTERED):
            self._set_status("approval")
        else:
            self._set_status("unavailable", reason="because macOS can't find the fan helper in this copy")
        return False

    def _hold(self, targets, temp, now):
        """Sets the speeds if they changed, or checks in. Returns whether the fans are held."""
        try:
            if targets != self._sent or not self._held:
                self._helper.request("set " + " ".join(map(str, targets)), timeout=40)
                if not self._held:
                    log.info("Fans held by %s at %s rpm (cores %s)", PROFILE_NAMES[self._pid],
                             ", ".join(map(str, targets)), "unknown" if temp is None else f"{temp:.1f}°")
                self._set_held(True, targets)
            elif self._helper.request("checkin") == "released":
                log.info("The fan helper had handed the fans back to macOS; taking them again")
                self._lost()
                return False
            return True
        except HelperError as e:
            self._failed(e.reason, now)
            return False

    def _failed(self, reason, now):
        self._lost()
        if reason == "asleep":
            return
        if reason == "other-app":
            self._retry_at = now + self.OTHER_APP_RETRY
            return self._set_status("other")
        if reason == "not-allowed":
            self._retry_at = now + 3600
            return self._set_status("unavailable", reason="because the fan helper didn't accept this copy's signature")
        if reason == "unreachable":
            self._next_check = 0.0  # perhaps it was turned off in Login Items
            text = "it couldn't reach the fan helper"
        else:
            text = f"the fan helper reported an error ({reason})"
        self._retry_at = now + self.RETRY_AFTER
        self._set_status("error", reason=text)

    def _release(self, why):
        if not self._held:
            return
        try:
            self._helper.request("auto", timeout=20)
            log.info("Fans back to macOS: %s", why)
        except HelperError as e:
            log.warning("Fans may not be back with macOS (%s): %s", e.reason, why)
        self._set_held(False, None)
        self._helper.close()  # the helper exits once nobody is connected

    def _lost(self):
        """The fans aren't held any more (or shouldn't be counted on): start over."""
        self._set_held(False, None)
        self._helper.close()
        self._safety.reset()
        if self._curve is not None:
            self._curve.reset()

    def _set_held(self, held, sent):
        """Holding the fans keeps the app's threads at full priority (priority.py), so this is
        where that is switched, at once, rather than at the next sample."""
        changed = held != self._held
        self._held, self._sent = held, sent
        if changed and self._priority:
            self._priority.refresh()

    def _set_status(self, state, **details):
        status = {"profile": self._pid or MACOS, "state": state, **details}
        if state != self._last_state:
            if self._last_state == "override":
                log.info("Safety override over")
            self._last_state = state
            if state == "unavailable":
                log.warning("Fan control isn't available %s", details.get("reason"))
            elif state == "error":
                log.warning("Fan control stopped because %s", details.get("reason"))
            elif state == "approval":
                log.info("Fan control is waiting for the helper to be allowed in Login Items")
            elif state == "other":
                log.info("Another app is controlling the fans; leaving them alone")
            elif state == "override":
                log.warning("Safety override: fans at full speed (%s)",
                            Safety.describe(details["reason"], details.get("pressure", 0)))
        self.status = status
