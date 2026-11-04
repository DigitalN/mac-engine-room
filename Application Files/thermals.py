"""Read temperatures and fans from the SMC, for the Thermals tool.

Read-only, through power.py's SMC class. Temperatures are read every sample, like
power; fans only while something shows them (see EngineRoomApp._thermal_parts). The
keys are found once per connection; after that a reading is one IOKit call per key
(about 30 µs of CPU, 220 µs waiting for the SMC, on an M1 Pro), so only the groups asked
for are read.

CPU and GPU are the hottest *core* sensor, from a per-chip table of core keys. The
chip's hotspot keys (TCDX, TCMb, TCMz…) and the helper Tp keys next to each core
key aren't cores, and TG0B and friends are the battery's gas gauge (on an M1 Pro
TG0B reads exactly TB0T), so none of them count. Memory, SSD and ambient are the
hottest sensor in their group. Readings of exactly 0, outside -40–130 °C, or from a part
of the chip that has powered down (see PARKED_BELOW) are ignored. While every GPU sensor
is parked, the reader keeps the last real reading in `gpu_held`, for display only.
"""

import ctypes
import logging
import math
import re
import time

import objc

import power

log = logging.getLogger(__name__)

# CPU and GPU core keys per chip: (CPU, GPU). One key per core: the middle key of each Tp/Te
# triplet and the second of each Tg pair, the way Stats picks them on the M1 (measured on
# this M1 Pro). From Stats' sensor list (github.com/exelban/stats,
# Modules/Sensors/values.swift at a0b1c12e8b, MIT), checked against the SMC dumps behind
# iSMC's table (github.com/dkorunic/iSMC, src/temp.txt, internal/reports). iSMC is GPL, so
# its dumps are evidence for which keys exist, not a source to copy. A name with a variant
# ("M4 Pro") wins over its generation ("M4"). Keys a Mac doesn't have are skipped.
_M1 = ("Tp09 Tp0T Tp01 Tp05 Tp0D Tp0H Tp0L Tp0P Tp0X Tp0b", "Tg05 Tg0D Tg0L Tg0T")
_M4_CPU = "Te05 Te0S Te09 Te0H Tp01 Tp05 Tp09 Tp0D Tp0V Tp0Y Tp0b Tp0e"
_M4_GPU = "Tg0K Tg0L Tg0d Tg0e Tg0j Tg0k"
# The M4 Pro and Max: Te06, Te0T and Tp0H (Stats #3270, measured on an M4 Pro, which has no
# Te09, Te0H or Tp0V), efficiency cores 3 and 4 (Tpx9, TpxC), the rest of the performance
# cores (Tp0L…Tp0X), and the second layout some M4 Pros use instead (Tp1j…Tp2E; never both).
_M4_PRO_CPU = (_M4_CPU + " Te06 Te0T Tpx9 TpxC Tp0H Tp0L Tp0P Tp0T Tp0X"
               " Tp1j Tp1n Tp1t Tp1w Tp1z Tp22 Tp25 Tp28 Tp2B Tp2E")
# The base M1 (MacBookAir10,1, from its sensor report, macOS 27.2) names its cores another way:
# four keys each, a/b/x/z, where z is x plus a whole number of degrees (a hotspot estimate,
# like an M1 Pro triplet's third key). TCMb equals the hottest b key, as on the M1 Pro it
# equals the hottest middle key, so b is each core's key. Its GPU keys read 0 while idle.
_M1_BASE = ("Te0b Te3b Tp2b Tp3b Tp4b Tp5b Tp7b Tp8b Tp9b", "Tg1b Tg4b")
CORE_KEYS = {
    "M1": (f"{_M1[0]} {_M1_BASE[0]}", f"{_M1[1]} {_M1_BASE[1]}"),
    # The Ultra's second die: the same cores at Tp2… where the first has Tp0…, and four more GPU pairs.
    "M1 Ultra": (_M1[0] + " Tp29 Tp2T Tp21 Tp25 Tp2D Tp2H Tp2L Tp2P Tp2X Tp2b", _M1[1] + " Tg0b Tg0j Tg0r Tg0z"),
    # Te05: the efficiency cluster, which has no per-core keys on some M2s. Tp0n, Tp0r and
    # the GPU pairs past Tg0j: the M2 Pro and Max (iSMC's dumps).
    "M2": ("Tp1h Tp1t Tp1p Tp1l Tp01 Tp05 Tp09 Tp0D Tp0X Tp0b Tp0f Tp0j Tp0n Tp0r Te05",
           "Tg0f Tg0j Tg0n Tg0r Tg0X Tg1h Tg1l Tg2X Tg2f Tg3Z"),
    # Stats counted Tf… as M3 cores, but they're the GPU's fabric (iSMC), and a base M3 has
    # none (Stats #1703), so the M3 read only its efficiency cores. These are its cores.
    "M3": ("Te05 Te0H Te0L Te0P Te0S Te0U Tp05 Tp0D Tp0L Tp0b Tp0h Tp0n Tp1F Tp1R Tp0z",
           "Tg05 Tg0D Tg0L Tg01 Tg0u Tg0v Tg13 Tg1B Tg1l Tg0z Tg1F Tg17 Tg1t Tg1y Tg22 Tg2A Tg2I Tg34 Tg3C Tg3K Tg3y"),
    "M4": (_M4_CPU, "Tg0G Tg0H " + _M4_GPU),
    "M4 Pro": (_M4_PRO_CPU, "Tg1U Tg1k " + _M4_GPU),
    "M4 Max": (_M4_PRO_CPU, "Tg1U Tg1k " + _M4_GPU),
    "M5": ("Tp00 Tp04 Tp08 Tp0C Tp0G Tp0K Tp0O Tp0R Tp0U Tp0X Tp0a Tp0d Tp0g Tp0j Tp0m Tp0p Tp0u Tp0y",
           "Tg0U Tg0X Tg0d Tg0g Tg0j Tg1Y Tg1c Tg1g"),
    "M6": ("Tp0j Tp0g Tp09 Tp07 Tp0m Tp0L Tp0I Tp0d Tp0E Tp0G Tp0b Tp05 Te07 Te08 Te09 Te0c Te0h Te0i",
           "Tg1e Tg0d Tg0c Tg0b Tg1c Tg1d Tg07 Tg1f Tg09 Tg0a Tg1g Tg1h"),
    "A18 Pro": ("Te05 Te0S Tp05 Tp0D", "Tg05 Tg0D Tg0L Tg0e"),
}
# Keys a group's prefix finds that aren't that group, per generation: on the M5, Tm0y and
# Tm2T copy a performance core's reading (iSMC's dumps).
NOT_IN_GROUP = {"M5": {"memory": ("Tm0y", "Tm2T")}}
# A chip the table doesn't know, or whose table keys don't fit this Mac (fewer than half as
# many CPU keys as it has cores), uses every key with these prefixes. That counts helper
# keys too, which read a few degrees hot under load: too warm, never too cool. GPU is
# lowercase Tg only; TG… is the battery.
FALLBACK_PREFIXES = {"cpu": ("Tp", "Te"), "gpu": ("Tg",)}
# Memory, SSD and ambient: the hottest key in the group. Ta… isn't ambient (several read 0).
GROUP_PREFIXES = {"memory": ("Tm",), "ssd": ("TH",), "ambient": ("TA",)}
# For fan control's safety override only, never shown: the chip's hotspot and cluster keys
# (TCDX, TCMb, TCMz, TC10…), judged by their second-highest reading, and TS0P, a sensor
# inside the Mac (next to the SSD, per iSMC; the M2, base M4 and A18 don't have it).
HOTSPOT_PREFIXES = ("TC",)
CHASSIS_KEY = "TS0P"

TEMPERATURES = ("cpu", "gpu", "memory", "ssd", "ambient")
CHIP_GROUPS = ("cpu", "gpu")
ALL_PARTS = frozenset(TEMPERATURES + ("fans",))
SAFETY = "safety"  # the hotspots and TS0P, read while fan control holds the fans
# A reading counts from -40° to 130°, except exactly 0, which is a sensor with nothing to say.
# Below freezing is real (a Mac used outside on a cold day). Parts of the chip that power
# down give made-up readings (-4°, 0°, 1.5–5°, or one value on every key, like a flat 40.0°
# on an M4 Pro: Stats #3271, iSMC, macmon), so a chip sensor more than PARKED_BELOW under
# the Mac's coolest other reading (ambient, memory or SSD) is parked: nothing on the chip
# can be cooler than the air and parts around it. Not the hottest CPU core: under load
# that's far above an idle GPU that's awake.
MIN_C, MAX_C = -40.0, 130.0
PARKED_BELOW = 10.0
MAX_RPM = 20000
SMC_RETRY_SECONDS = 30.0


def chip_name():
    """Like "Apple M1 Pro". It names the kind of chip, not this Mac."""
    size = ctypes.c_size_t(128)
    value = ctypes.create_string_buffer(size.value)
    rc = power._libc.sysctlbyname(b"machdep.cpu.brand_string", value, ctypes.byref(size), None, 0)
    return value.value.decode("ascii", "replace") if rc == 0 else ""


def core_count():
    """The CPU's cores (hw.ncpu), or 0 if it can't be read."""
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    rc = power._libc.sysctlbyname(b"hw.ncpu", ctypes.byref(value), ctypes.byref(size), None, 0)
    return value.value if rc == 0 else 0


def _family(chip):
    """("M4", "Pro") for "Apple M4 Pro", ("M4", None) for "Apple M4", or None."""
    m = re.match(r"Apple (M\d+|A\d+)(?: (Pro|Max|Ultra))?\b", chip or "")
    return (m.group(1), m.group(2)) if m else None


def core_table(chip):
    """(CPU keys, GPU keys) for a chip name like "Apple M4 Pro", or None if it's not in the table."""
    found = _family(chip)
    if not found:
        return None
    family, variant = found
    entry = CORE_KEYS.get(f"{family} {variant}") or CORE_KEYS.get(family)
    return tuple(tuple(keys.split()) for keys in entry) if entry else None


def count_fans(names):
    """Fans with a speed key, F0Ac, F1Ac… in a row: for a Mac that doesn't say in FNum."""
    return next(i for i in range(10) if f"F{i}Ac" not in names)


def sensor_keys(names, chip, fan_count=0, cores=0):
    """The keys to read on this Mac, by group, from the names its SMC lists.

    Returns {"cpu": (...), "gpu": (...), "memory": (...), "ssd": (...), "ambient": (...),
    "fans": ((actual, minimum, maximum, mode), ...), "hotspots", "chassis", "by_prefix"}. A
    group this Mac has no key for is empty. CPU or GPU comes from the prefixes instead of
    the table where the chip isn't in it, or the table doesn't fit this Mac (a variant it
    doesn't know yet): no GPU key, or fewer CPU keys than half its `cores` (0: unknown, so
    only none counts). "by_prefix" names the groups that did.
    """
    names = set(names)
    table = core_table(chip) or ((), ())
    keys = {"by_prefix": ()}
    for group, listed in zip(CHIP_GROUPS, table):
        found = tuple(k for k in listed if k in names)
        if not found or (group == "cpu" and cores and len(found) * 2 < cores):
            found = tuple(sorted(n for n in names if n.startswith(FALLBACK_PREFIXES[group])))
            keys["by_prefix"] += (group,) if found else ()
        keys[group] = found
    excluded = NOT_IN_GROUP.get((_family(chip) or ("",))[0], {})
    for group, prefixes in GROUP_PREFIXES.items():
        keys[group] = tuple(sorted(n for n in names if n.startswith(prefixes) and n not in excluded.get(group, ())))
    keys["fans"] = tuple(
        (f"F{i}Ac", f"F{i}Mn", f"F{i}Mx", f"F{i}md" if f"F{i}md" in names else f"F{i}Md")
        for i in range(fan_count))
    keys["hotspots"] = tuple(sorted(n for n in names if n.startswith(HOTSPOT_PREFIXES)))
    keys["chassis"] = (CHASSIS_KEY,) if CHASSIS_KEY in names else ()
    return keys


def _temperature(value):
    return isinstance(value, (int, float)) and value != 0 and MIN_C <= value <= MAX_C


def _chip_readings(values, coolest):
    """A chip group's readings without the parked ones (see PARKED_BELOW)."""
    if len(values) >= 3 and len(set(values)) == 1:
        return []  # every key on one value: made up
    return values if coolest is None else [v for v in values if v >= coolest - PARKED_BELOW]


def _rpm(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= MAX_RPM:
        return None
    return int(round(value))


def normalize(readings, keys, now=None):
    """Turn SMC readings ({key: value}) into a sample. Pure; unit-tested with captured keys.

    Temperatures are °C, None where nothing was read or every reading was out of range or
    parked. `sensors` counts the readings each one is the hottest of. A fan's `manual` is
    True when something other than macOS sets its speed (mode 1; macOS uses 0 or 3).
    `gpu_held` is for the reader to fill in (the last real GPU reading while it's parked).
    """
    sample = {"t": time.time() if now is None else now, "sensors": {}, "gpu_held": None}
    others = [g for g in TEMPERATURES if g not in CHIP_GROUPS]
    for group in others + list(CHIP_GROUPS):
        valid = [readings[k] for k in keys.get(group, ()) if _temperature(readings.get(k))]
        if group in CHIP_GROUPS:
            around = [sample[g] for g in others if sample[g] is not None]
            valid = _chip_readings(valid, min(around) if around else None)
        sample[group] = round(max(valid), 1) if valid else None
        sample["sensors"][group] = len(valid)
    sample["sensors"] = {g: sample["sensors"][g] for g in TEMPERATURES}
    present = [sample[g] for g in TEMPERATURES if sample[g] is not None]
    sample["hottest"] = max(present) if present else None
    fans = []
    for actual, minimum, maximum, mode in keys.get("fans", ()):
        if actual not in readings:
            continue
        md = readings.get(mode)
        fans.append({"rpm": _rpm(readings[actual]), "min": _rpm(readings.get(minimum)),
                     "max": _rpm(readings.get(maximum)), "manual": None if md is None else md == 1})
    sample["fans"] = fans
    hotspots = sorted((readings[k] for k in keys.get("hotspots", ()) if _temperature(readings.get(k))), reverse=True)
    sample["hotspot"] = round(hotspots[1], 1) if len(hotspots) > 1 else None  # one bad sensor can't decide
    chassis = [readings[k] for k in keys.get("chassis", ()) if _temperature(readings.get(k))]
    sample["chassis"] = round(chassis[0], 1) if chassis else None
    return sample


class ThermalsReader:
    """Owns its own SMC connection. Finds the keys the first time it's asked for a reading,
    and again after a reconnect; call read() with the parts that are needed."""

    def __init__(self):
        self._smc = None
        self._retry_at = 0.0
        self._keys = None
        self._fan_limits = {}  # minimum and maximum speeds don't change: read once
        self._chip = chip_name()
        self._cores = core_count()
        self._gpu_seen = None  # the last real GPU reading: (°C, when, sensors)
        self.available = ()  # the parts this Mac has: TEMPERATURES with a key, then "fans"
        # What the app had to guess on this Mac ("cpu", "gpu": by prefix, or no GPU key),
        # for Settings' hint that a sensor report would help.
        self.guessing = ()

    def _connect(self):
        try:
            smc = power.SMC()
            names = smc.key_names()
            fan_count = int(smc.read("FNum") or 0) or count_fans(names)
            keys = sensor_keys(names, self._chip, fan_count, self._cores)
            limits = {k: smc.read(k) for fan in keys["fans"] for k in fan[1:3]}
        except OSError as e:
            log.warning("SMC unavailable for temperatures (%s)", e)
            self._retry_at = time.monotonic() + SMC_RETRY_SECONDS
            return
        self._smc, self._keys, self._fan_limits = smc, keys, limits
        self.available = tuple(g for g in TEMPERATURES if keys[g]) + (("fans",) if keys["fans"] else ())
        self.guessing = keys["by_prefix"] + (() if keys["gpu"] else ("gpu",))
        by_prefix = ", ".join(keys["by_prefix"])
        log.info("Temperature sensors on %s: %s; %d fan%s%s%s", self._chip or "an unknown chip",
                 ", ".join(f"{g} {len(keys[g])}" for g in TEMPERATURES), fan_count, "" if fan_count == 1 else "s",
                 f"; not in the core key table, so by prefix: {by_prefix}" if by_prefix else "",
                 "; no GPU sensor" if not keys["gpu"] else "")

    def fan_limits(self):
        """Each fan's (minimum, maximum) rpm, or [] if there are no fans, they're unknown, or
        nothing has been read yet. Safe from any thread: it only looks at what read() found."""
        keys, found = self._keys, self._fan_limits
        limits = [(_rpm(found.get(lo)), _rpm(found.get(hi))) for _, lo, hi, _ in (keys or {}).get("fans", ())]
        return limits if limits and all(lo and hi and hi > lo for lo, hi in limits) else []

    def read(self, parts=ALL_PARTS):
        """A sample with the parts asked for ("cpu", "gpu", …, "fans", "safety"); the rest are
        None or empty. None if the SMC can't be read."""
        if self._smc is None and time.monotonic() >= self._retry_at:
            self._connect()
        if self._smc is None:
            return None
        with objc.autorelease_pool():
            wanted = [k for g in TEMPERATURES if g in parts for k in self._keys[g]]
            fans = self._keys["fans"] if "fans" in parts else ()
            wanted += [k for actual, _, _, mode in fans for k in (actual, mode)]
            safety = {g: self._keys[g] if SAFETY in parts else () for g in ("hotspots", "chassis")}
            wanted += [k for g in safety.values() for k in g]
            try:
                readings = self._smc.read_all(wanted)
            except OSError as e:
                log.warning("SMC temperature read failed (%s); reconnecting", e)
                self._smc.close()
                self._smc = None
                self._retry_at = time.monotonic() + SMC_RETRY_SECONDS
                return None
            readings.update(self._fan_limits)
            keys = {g: self._keys[g] if g in parts else () for g in TEMPERATURES}
            sample = normalize(readings, {**keys, "fans": fans, **safety})
            sample["available"] = list(self.available)
            # A parked GPU shows its last real reading, for the page and the menu only: the
            # fan curve and the "hottest" title use "gpu", which stays None.
            if keys["gpu"]:
                if sample["gpu"] is not None:
                    self._gpu_seen = (sample["gpu"], sample["t"], sample["sensors"]["gpu"])
                elif self._gpu_seen:
                    c, t, n = self._gpu_seen
                    sample["gpu_held"] = {"c": c, "t": t, "sensors": n}
            return sample
