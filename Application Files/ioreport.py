"""Chip power from IOReport, for Macs whose SMC has no working PHPC key (the 13" M4 Air, #51).

IOReport is the private library behind powermetrics. It counts the energy the CPU, GPU and
Neural Engine have used, readable without root on any Apple silicon Mac. Only PowerReader opens
it, and only where PHPC can't give Chip, so no other Mac pays for it.

Two sources, read the way macmon reads them (MIT, https://github.com/vladkens/macmon):
  - macOS 27: AppleCLPC's energy counters. The "Energy Model" group's CPU and ANE counters stop
    counting on 27 without an Apple entitlement (macmon; measured on the M1 Pro on 27.2: CPU
    Energy's delta was 0 while PHPC read 10 to 30 W, and only GPU Energy moved). CLPC's
    counters aren't listed, so their channels are made here from the driver's registry ID and
    the report IDs in CLPC_REPORTS.
  - Any other macOS: the Energy Model's CPU Energy, GPU Energy and ANE channels.
Both count the CPU, GPU and Neural Engine only. PHPC is the whole package, which has more on it,
so where both work IOReport reads lower: a steady 7 to 8 W less on the M1 Pro (medians of 20 s,
9.3 W against PHPC's 16.8 W, and 4.0 W against 13.5 W).

The counters are cumulative, so a reading is the energy since the last one over the time between
them: the first gives none. Each channel says its own unit (mJ, uJ or nJ).
"""

import ctypes
import ctypes.util
import time

import objc
from Foundation import NSArray, NSMutableDictionary

# Joules per unit, by the unit label a channel gives.
JOULES = {"mJ": 1e-3, "uJ": 1e-6, "µJ": 1e-6, "nJ": 1e-9}
PARTS = ("cpu", "gpu", "ane")
# A running Mac's CPU always uses some energy, but the counters only move every 5 to 10 ms
# (CLPC's on the M1 Pro), so two readings close together can see no change. One that hasn't
# moved for this long isn't counting: it gives no reading, and if it never has, IOReport is
# given up on.
FROZEN_SECONDS = 5.0

# AppleCLPC's CPU, GPU and ANE energy counters on macOS 27, by driver bundle: the low 32 bits of
# each report ID. From macmon's src/clpc.rs (MIT), which extracted them from macOS 27.0 and
# 27.0.1 restore images. The full ID puts the counter's table index (CLPC_INDICES_27) above them.
CLPC_REPORTS = {
    "com.apple.driver.AppleT6000CLPCv3": (0x47479059, 0x4519BF1E, 0x9AAA17F8),  # M1 Pro, M1 Max
    "com.apple.driver.AppleT6002CLPC": (0xB501816B, 0x9E2C3E8B, 0x14D5A574),  # M1 Ultra
    "com.apple.driver.AppleT6020CLPC": (0xE301CD75, 0x8F34CC7A, 0x7415FB0F),  # M2 Pro, M2 Max
    "com.apple.driver.AppleT6022CLPC": (0x76F5FD21, 0x90624499, 0x3C097307),  # M2 Ultra
    "com.apple.driver.AppleT6030CLPC": (0xBE03A01A, 0x472D6C9C, 0xB8990C38),  # M3 Pro
    "com.apple.driver.AppleT6031CLPC": (0xD81E0084, 0x1085F98B, 0x313A3BE1),  # M3 Max
    "com.apple.driver.AppleT6032CLPC": (0xF4A21308, 0x85E80513, 0x63DC8E25),  # M3 Ultra
    "com.apple.driver.AppleT6041CLPC": (0x263CF1F0, 0x22E4CF59, 0x7849FB5C),  # M4 Pro, M4 Max
    "com.apple.driver.AppleT6050CLPC": (0xD3AB60BB, 0x27CD4CE5, 0x8C0F1C59),
    "com.apple.driver.AppleT6050dCLPC": (0x18B3010E, 0xEDECA11A, 0x188BD883),
    "com.apple.driver.AppleT8103CLPCv3": (0x2CDD31F2, 0x21BECF42, 0x0667FB81),  # M1
    "com.apple.driver.AppleT8112CLPC": (0xB5435137, 0x638D9D52, 0xEA089C36),  # M2
    "com.apple.driver.AppleT8122CLPC": (0x54B51C99, 0xCE2DD9DC, 0x960AFE2F),  # M3
    "com.apple.driver.AppleT8132CLPC": (0x4A854D94, 0x3B3E3B79, 0x1FF4A9EC),  # M4
    "com.apple.driver.AppleT8140CLPC": (0x9BF54436, 0xEF70589C, 0x569D941B),
    "com.apple.driver.AppleT8142CLPC": (0x43708744, 0x6A010933, 0xBC5EFDF0),  # M5
    "com.apple.driver.AppleT8152CLPC": (0x485D4BFC, 0x11784654, 0x9380C6BB),
}
CLPC_INDICES_27 = (16, 25, 24)  # CPU, GPU, ANE. On 26 GPU was 24 and ANE 23 (macmon), so 28 may move them too.
CLPC_NAMES = ("CPU Energy", "GPU Energy", "ANE")
CLPC_PARTS = dict(zip(CLPC_NAMES, PARTS))
# What ChipEnergy.source says, for the log and the sensor report.
CLPC_SOURCE, ENERGY_MODEL_SOURCE = "CLPC energy counters", "Energy Model"


# ── Pure parts (unit-tested) ──────────────────────────────────────────────

def energy_model_part(name):
    """Which part of the chip an Energy Model channel counts: 'cpu', 'gpu', 'ane' or None.

    Matched as macmon matches them, since the names differ between chips: "CPU Energy", and
    "DIE_0_CPU Energy" for each die of an Ultra; "GPU Energy"; "ANE", "ANE0" on a Max and
    "ANE0_0" on an Ultra. The per-cluster and per-core CPU channels ("PACC0_CPU", "EACC_CPU1")
    and "GPU0" are parts of those totals, so they're left out.
    """
    if name == "GPU Energy":
        return "gpu"
    if name.endswith("CPU Energy"):
        return "cpu"
    if name.startswith("ANE"):
        return "ane"
    return None


def clpc_channels(bundle, macos):
    """[(report ID, name, part)] for this AppleCLPC driver on this macOS, or None.

    Only on macOS 27, whose tables these are; a driver they don't list gets none.
    """
    keys = CLPC_REPORTS.get(bundle)
    if keys is None or (macos or "").split(".")[0] != "27":
        return None
    return [((index << 32) | key, name, part)
            for index, key, name, part in zip(CLPC_INDICES_27, keys, CLPC_NAMES, PARTS)]


def energy_joules(value, unit):
    """A counter's value in joules, or None for a unit it doesn't know."""
    scale = JOULES.get((unit or "").strip())
    return None if scale is None else value * scale


def counter_deltas(before, after):
    """[(part, energy used, unit)] between two readings of the counters, each {channel name:
    (part, cumulative value, unit)}. A channel missing from either is left out."""
    return [(part, value - before[name][1], unit)
            for name, (part, value, unit) in after.items() if name in before]


def cpu_moved(deltas):
    return any(part == "cpu" and value > 0 for part, value, _ in deltas)


def chip_watts(deltas, seconds):
    """Chip power (W) from one reading's counter deltas, [(part, value, unit)] over `seconds`.

    CPU + GPU + ANE: ANE reads 0 unless something uses the Neural Engine, and it's on the chip,
    like everything PHPC counts. None when it can't be trusted: no CPU or GPU channel, a unit it
    doesn't know, a counter that went backwards, or CPU energy that didn't move (a frozen counter,
    like the Energy Model's on macOS 27).
    """
    if not seconds or seconds <= 0:
        return None
    totals = dict.fromkeys(PARTS, 0.0)
    seen = set()
    for part, value, unit in deltas:
        joules = energy_joules(value, unit)
        if part not in totals or joules is None or joules < 0:
            return None
        totals[part] += joules
        seen.add(part)
    if not {"cpu", "gpu"} <= seen or totals["cpu"] <= 0:
        return None
    return sum(totals.values()) / seconds


# ── IOReport and CoreFoundation via ctypes ────────────────────────────────

_vp, _u64 = ctypes.c_void_p, ctypes.c_uint64
_UTF8 = 0x08000100
_lib_cache = {}


def _libs():
    """(IOReport, CoreFoundation, IOKit, libc), loaded the first time. OSError if IOReport isn't there."""
    if not _lib_cache:
        try:
            ior = ctypes.CDLL("/usr/lib/libIOReport.dylib")  # in the shared cache, not on disk
            ior.IOReportCopyChannelsInGroup.restype = _vp
            ior.IOReportCopyChannelsInGroup.argtypes = [_vp, _vp, _u64, _u64, _u64]
            ior.IOReportCreateSubscription.restype = _vp
            ior.IOReportCreateSubscription.argtypes = [_vp, _vp, ctypes.POINTER(_vp), _u64, _vp]
            ior.IOReportCreateSamples.restype = _vp
            ior.IOReportCreateSamples.argtypes = [_vp, _vp, _vp]
            # These return strings the channel dictionary keeps: not to be released.
            ior.IOReportChannelGetChannelName.restype = _vp
            ior.IOReportChannelGetChannelName.argtypes = [_vp]
            ior.IOReportChannelGetUnitLabel.restype = _vp
            ior.IOReportChannelGetUnitLabel.argtypes = [_vp]
            ior.IOReportSimpleGetIntegerValue.restype = ctypes.c_int64
            ior.IOReportSimpleGetIntegerValue.argtypes = [_vp, ctypes.c_int32]
        except (OSError, AttributeError) as e:
            raise OSError(f"libIOReport won't load ({e})") from None
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cf.CFRelease.argtypes = [_vp]
        cf.CFStringCreateWithCString.restype = _vp
        cf.CFStringCreateWithCString.argtypes = [_vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [_vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFDictionaryGetValue.restype = _vp
        cf.CFDictionaryGetValue.argtypes = [_vp, _vp]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [_vp]
        cf.CFArrayGetValueAtIndex.restype = _vp
        cf.CFArrayGetValueAtIndex.argtypes = [_vp, ctypes.c_long]
        iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        iokit.IOServiceMatching.restype = _vp
        iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
        iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, _vp]
        iokit.IORegistryEntryGetRegistryEntryID.restype = ctypes.c_int
        iokit.IORegistryEntryGetRegistryEntryID.argtypes = [ctypes.c_uint32, ctypes.POINTER(_u64)]
        iokit.IORegistryEntryCreateCFProperty.restype = _vp
        iokit.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, _vp, _vp, ctypes.c_uint32]
        iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        libc = ctypes.CDLL(None)
        _lib_cache.update(ior=ior, cf=cf, iokit=iokit, libc=libc,
                          channels_key=cf.CFStringCreateWithCString(None, b"IOReportChannels", _UTF8))
    return _lib_cache


def _string(cf, ref):
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(128)
    return buf.value.decode("utf-8", "replace") if cf.CFStringGetCString(ref, buf, 128, _UTF8) else ""


def macos_version():
    """macOS's version, like 27.2, from the kernel: unlike platform.mac_ver(), never the 10.16
    an old SDK's compatibility mode reports."""
    libc = _libs()["libc"]
    size = ctypes.c_size_t(32)
    buf = ctypes.create_string_buffer(size.value)
    return buf.value.decode() if libc.sysctlbyname(b"kern.osproductversion", buf, ctypes.byref(size), None, 0) == 0 else ""


def _clpc_driver():
    """(registry ID, bundle ID) of this Mac's AppleCLPC driver, or None."""
    libs = _libs()
    iokit, cf = libs["iokit"], libs["cf"]
    service = iokit.IOServiceGetMatchingService(0, iokit.IOServiceMatching(b"AppleCLPC"))  # consumes the dict
    if not service:
        return None
    try:
        entry_id = _u64()
        if iokit.IORegistryEntryGetRegistryEntryID(service, ctypes.byref(entry_id)) != 0:
            return None
        key = cf.CFStringCreateWithCString(None, b"CFBundleIdentifier", _UTF8)
        ref = iokit.IORegistryEntryCreateCFProperty(service, key, None, 0)
        cf.CFRelease(key)
        if not ref:
            return None
        try:
            return entry_id.value, _string(cf, ref)
        finally:
            cf.CFRelease(ref)
    finally:
        iokit.IOObjectRelease(service)


def _clpc_channel(driver, report_id, name):
    """A CLPC energy counter's channel, as IOReport describes one (macmon's clpc_energy_channel):
    a simple integer in the power category, one element, in nJ."""
    return NSMutableDictionary.dictionaryWithDictionary_({
        "DriverID": driver,
        "DriverName": "AppleCLPC",
        "IOReportGroupName": "CLPC",
        "IOReportSubGroupName": "Energy Counters",
        "IOReportChannelInfo": {"IOReportChannelUnit": (3 << 56) | (118 << 32)},
        "LegendChannel": [report_id, 1 | (2 << 16) | (1 << 32), name],
    })


class ChipEnergy:
    """A subscription to the chip's energy counters, kept for the reader's life.

    Raises OSError, saying why, when IOReport can't give Chip power here. read() gives watts,
    or None for the first reading and for one that can't be trusted, and raises OSError once
    the CPU counter turns out not to count. The caller holds an autorelease pool.
    """

    def __init__(self):
        libs = _libs()
        self._ior, self._cf = libs["ior"], libs["cf"]
        self._sub = self._subbed = self._prev = self._watts = None
        self._held = None  # the channel descriptions, kept alive while IOReport may use them
        self._cpu_moved = False
        macos = macos_version()
        driver = _clpc_driver()
        channels = clpc_channels(driver[1], macos) if driver else None
        if channels:
            self.source = CLPC_SOURCE
            self._held = desired = NSMutableDictionary.dictionaryWithDictionary_({
                "IOReportChannels": NSArray.arrayWithArray_(
                    [_clpc_channel(driver[0], report_id, name) for report_id, name, _ in channels])})
        elif macos.split(".")[0] == "27":
            # The Energy Model's CPU counter doesn't count on 27, so it can't stand in.
            raise OSError(f"no CLPC energy counters known for {driver[1] if driver else 'this Mac'} on macOS {macos}")
        else:
            self.source = ENERGY_MODEL_SOURCE
            self._held = desired = self._energy_model_channels()
        subbed = _vp()
        self._sub = self._ior.IOReportCreateSubscription(None, objc.pyobjc_id(desired), ctypes.byref(subbed), 0, None)
        self._subbed = subbed.value
        if not self._sub or not self._subbed:
            self.close()
            raise OSError(f"couldn't subscribe to its {self.source}")

    def _energy_model_channels(self):
        group = self._cf.CFStringCreateWithCString(None, b"Energy Model", _UTF8)
        found = self._ior.IOReportCopyChannelsInGroup(group, None, 0, 0, 0)
        self._cf.CFRelease(group)
        if not found:
            raise OSError("no Energy Model channels")
        try:
            listed = self._cf.CFDictionaryGetValue(found, _libs()["channels_key"])
            wanted, parts = [], set()
            for i in range(self._cf.CFArrayGetCount(listed) if listed else 0):
                item = self._cf.CFArrayGetValueAtIndex(listed, i)
                part = energy_model_part(_string(self._cf, self._ior.IOReportChannelGetChannelName(item)))
                if part:
                    wanted.append(objc.objc_object(c_void_p=item))
                    parts.add(part)
            if not {"cpu", "gpu"} <= parts:
                raise OSError("the Energy Model has no CPU or no GPU channel")
            # Only the channels it needs: each one costs a little on every reading.
            desired = NSMutableDictionary.dictionaryWithDictionary_(objc.objc_object(c_void_p=found))
            desired["IOReportChannels"] = NSArray.arrayWithArray_(wanted)
            return desired
        finally:
            self._cf.CFRelease(found)

    def _counters(self):
        """{channel name: (part, cumulative value, unit)} now, or None. By name, never by
        position: a subscription can leave channels out and a sample can reorder them."""
        cf, ior = self._cf, self._ior
        sample = ior.IOReportCreateSamples(self._sub, self._subbed, None)
        if not sample:
            return None
        try:
            listed = cf.CFDictionaryGetValue(sample, _libs()["channels_key"])
            counters = {}
            for i in range(cf.CFArrayGetCount(listed) if listed else 0):
                item = cf.CFArrayGetValueAtIndex(listed, i)
                name = _string(cf, ior.IOReportChannelGetChannelName(item))
                part = CLPC_PARTS.get(name) if self.source == CLPC_SOURCE else energy_model_part(name)
                if part:
                    counters[name] = (part, ior.IOReportSimpleGetIntegerValue(item, 0),
                                      _string(cf, ior.IOReportChannelGetUnitLabel(item)))
            return counters
        finally:
            cf.CFRelease(sample)

    def read(self):
        """Watts since the last reading the CPU counter moved in, or None."""
        if not self._sub:
            return None
        counters, now = self._counters(), time.monotonic()
        if counters is None:
            return None
        if self._prev is None:
            self._prev = counters, now
            return None
        before, then = self._prev
        deltas = counter_deltas(before, counters)
        if not cpu_moved(deltas):
            # Too soon for the counters to move (the menu woke the sampler early): the last
            # reading stands, and the next one spans the whole time. Or it isn't counting.
            if now - then < FROZEN_SECONDS:
                return self._watts
            if not self._cpu_moved:
                self.close()
                raise OSError(f"the CPU counter isn't counting ({self.source})")
            self._watts = None
            return None
        self._prev, self._cpu_moved = (counters, now), True
        self._watts = chip_watts(deltas, now - then)
        return self._watts

    def close(self):
        for ref in (self._subbed, self._sub):
            if ref:
                self._cf.CFRelease(ref)
        self._sub = self._subbed = self._prev = self._held = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
