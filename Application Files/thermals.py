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
hottest sensor in their group. Readings of 0 or outside 1–130 °C are ignored.
"""

import ctypes
import logging
import math
import re
import time

import objc

import power

log = logging.getLogger(__name__)

# CPU and GPU core keys per chip: (CPU, GPU). From Stats' sensor list
# (github.com/exelban/stats, Modules/Sensors/values.swift at a0b1c12e8b, MIT). A name
# with a variant ("M4 Pro") wins over its generation ("M4"). Keys a Mac doesn't have
# are skipped.
_M4_CPU = "Te05 Te0S Te09 Te0H Tp01 Tp05 Tp09 Tp0D Tp0V Tp0Y Tp0b Tp0e"
_M4_GPU = "Tg0K Tg0L Tg0d Tg0e Tg0j Tg0k"
CORE_KEYS = {
    "M1": ("Tp09 Tp0T Tp01 Tp05 Tp0D Tp0H Tp0L Tp0P Tp0X Tp0b", "Tg05 Tg0D Tg0L Tg0T"),
    "M2": ("Tp1h Tp1t Tp1p Tp1l Tp01 Tp05 Tp09 Tp0D Tp0X Tp0b Tp0f Tp0j", "Tg0f Tg0j"),
    "M3": ("Te05 Te0L Te0P Te0S Tf04 Tf09 Tf0A Tf0B Tf0D Tf0E Tf44 Tf49 Tf4A Tf4B Tf4D Tf4E",
           "Tf14 Tf18 Tf19 Tf1A Tf24 Tf28 Tf29 Tf2A"),
    "M4": (_M4_CPU, "Tg0G Tg0H " + _M4_GPU),
    "M4 Pro": (_M4_CPU, "Tg1U Tg1k " + _M4_GPU),
    "M4 Max": (_M4_CPU, "Tg1U Tg1k " + _M4_GPU),
    "M5": ("Tp00 Tp04 Tp08 Tp0C Tp0G Tp0K Tp0O Tp0R Tp0U Tp0X Tp0a Tp0d Tp0g Tp0j Tp0m Tp0p Tp0u Tp0y",
           "Tg0U Tg0X Tg0d Tg0g Tg0j Tg1Y Tg1c Tg1g"),
    "M6": ("Tp0j Tp0g Tp09 Tp07 Tp0m Tp0L Tp0I Tp0d Tp0E Tp0G Tp0b Tp05 Te07 Te08 Te09 Te0c Te0h Te0i",
           "Tg1e Tg0d Tg0c Tg0b Tg1c Tg1d Tg07 Tg1f Tg09 Tg0a Tg1g Tg1h"),
    "A18 Pro": ("Te05 Te0S Tp05 Tp0D", "Tg05 Tg0D Tg0L Tg0e"),
}
# A chip the table doesn't know uses every key with these prefixes. That counts helper
# keys too, which read a few degrees hot under load: too warm, never too cool. GPU is
# lowercase Tg only; TG… is the battery.
FALLBACK_PREFIXES = {"cpu": ("Tp", "Te"), "gpu": ("Tg",)}
# Memory, SSD and ambient: the hottest key in the group. Ta… isn't ambient (several read 0).
GROUP_PREFIXES = {"memory": ("Tm",), "ssd": ("TH",), "ambient": ("TA",)}

TEMPERATURES = ("cpu", "gpu", "memory", "ssd", "ambient")
ALL_PARTS = frozenset(TEMPERATURES + ("fans",))
MIN_C, MAX_C = 1.0, 130.0
MAX_RPM = 20000
SMC_RETRY_SECONDS = 30.0


def chip_name():
    """Like "Apple M1 Pro". It names the kind of chip, not this Mac."""
    size = ctypes.c_size_t(128)
    value = ctypes.create_string_buffer(size.value)
    rc = power._libc.sysctlbyname(b"machdep.cpu.brand_string", value, ctypes.byref(size), None, 0)
    return value.value.decode("ascii", "replace") if rc == 0 else ""


def core_table(chip):
    """(CPU keys, GPU keys) for a chip name like "Apple M4 Pro", or None if it's not in the table."""
    m = re.match(r"Apple (M\d+|A\d+)(?: (Pro|Max|Ultra))?\b", chip or "")
    if not m:
        return None
    family, variant = m.group(1), m.group(2)
    entry = CORE_KEYS.get(f"{family} {variant}") or CORE_KEYS.get(family)
    return tuple(tuple(keys.split()) for keys in entry) if entry else None


def sensor_keys(names, chip, fan_count=0):
    """The keys to read on this Mac, by group, from the names its SMC lists.

    Returns {"cpu": (...), "gpu": (...), "memory": (...), "ssd": (...), "ambient": (...),
    "fans": ((actual, minimum, maximum, mode), ...)}. A group this Mac has no key for is
    empty. Where the table names a chip but this Mac has none of its keys for CPU or GPU
    (a variant it doesn't know yet), that one falls back to the prefixes too.
    """
    names = set(names)
    table = core_table(chip) or ((), ())
    keys = {}
    for group, listed in zip(("cpu", "gpu"), table):
        found = tuple(k for k in listed if k in names)
        keys[group] = found or tuple(sorted(n for n in names if n.startswith(FALLBACK_PREFIXES[group])))
    for group, prefixes in GROUP_PREFIXES.items():
        keys[group] = tuple(sorted(n for n in names if n.startswith(prefixes)))
    keys["fans"] = tuple(
        (f"F{i}Ac", f"F{i}Mn", f"F{i}Mx", f"F{i}md" if f"F{i}md" in names else f"F{i}Md")
        for i in range(fan_count))
    return keys


def _temperature(value):
    return isinstance(value, (int, float)) and MIN_C <= value <= MAX_C


def _rpm(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= MAX_RPM:
        return None
    return int(round(value))


def normalize(readings, keys, now=None):
    """Turn SMC readings ({key: value}) into a sample. Pure; unit-tested with captured keys.

    Temperatures are °C, None where nothing was read or every reading was out of range.
    `sensors` counts the readings each one is the hottest of. A fan's `manual` is True
    when something other than macOS sets its speed (mode 1; macOS uses 0 or 3).
    """
    sample = {"t": time.time() if now is None else now, "sensors": {}}
    for group in TEMPERATURES:
        valid = [readings[k] for k in keys.get(group, ()) if _temperature(readings.get(k))]
        sample[group] = round(max(valid), 1) if valid else None
        sample["sensors"][group] = len(valid)
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
        self.available = ()  # the parts this Mac has: TEMPERATURES with a key, then "fans"

    def _connect(self):
        try:
            smc = power.SMC()
            names = smc.key_names()
            fan_count = int(smc.read("FNum") or 0)
            keys = sensor_keys(names, self._chip, fan_count)
            limits = {k: smc.read(k) for fan in keys["fans"] for k in fan[1:3]}
        except OSError as e:
            log.warning("SMC unavailable for temperatures (%s)", e)
            self._retry_at = time.monotonic() + SMC_RETRY_SECONDS
            return
        self._smc, self._keys, self._fan_limits = smc, keys, limits
        self.available = tuple(g for g in TEMPERATURES if keys[g]) + (("fans",) if keys["fans"] else ())
        table = core_table(self._chip) or ((), ())
        by_prefix = [g for g, listed in zip(("cpu", "gpu"), table) if keys[g] and not set(keys[g]) <= set(listed)]
        log.info("Temperature sensors on %s: %s; %d fan%s%s", self._chip or "an unknown chip",
                 ", ".join(f"{g} {len(keys[g])}" for g in TEMPERATURES), fan_count, "" if fan_count == 1 else "s",
                 f"; not in the core key table, so by prefix: {', '.join(by_prefix)}" if by_prefix else "")

    def read(self, parts=ALL_PARTS):
        """A sample with the parts asked for ("cpu", "gpu", …, "fans"); the rest are None or
        empty. None if the SMC can't be read."""
        if self._smc is None and time.monotonic() >= self._retry_at:
            self._connect()
        if self._smc is None:
            return None
        with objc.autorelease_pool():
            wanted = [k for g in TEMPERATURES if g in parts for k in self._keys[g]]
            fans = self._keys["fans"] if "fans" in parts else ()
            wanted += [k for actual, _, _, mode in fans for k in (actual, mode)]
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
            sample = normalize(readings, {**keys, "fans": fans})
            sample["available"] = list(self.available)
            return sample
