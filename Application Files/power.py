"""Read Mac power telemetry from IOKit (AppleSmartBattery) and the SMC.

Read-only. Uses public IOKit registry properties plus SMC sensor keys — no
private frameworks, no root, no network. Serial numbers and other identifying
registry values are never read. The charge limit set in System Settings comes
from powerd's world-readable charging prefs.

Two sources, because neither is complete on its own:
  - SMC updates every second: system/adapter/screen/chip power, battery temp.
    Which keys exist depends on the Mac, so one it can't read is logged at launch.
  - AppleSmartBattery (IORegistry) is authoritative for battery state, capacity
    and adapter details, but its power telemetry only refreshes every ~30 s.

macOS 27 removed AppleRawCurrentCapacity, AppleRawMaxCapacity, DesignCapacity,
AbsoluteCapacity and Temperature from the top level of AppleSmartBattery; the
mAh values now live in the nested BatteryData dict. `normalize()` reads the
nested dict first and falls back to the old top-level keys, and treats every
key as optional so a future schema change degrades a field instead of the app.
"""

import ctypes
import ctypes.util
import logging
import os
import platform
import plistlib
import re
import struct
import time

import objc

log = logging.getLogger(__name__)

# Registry keys we read. Deliberately excludes Serial, AdapterDetails.SerialString, etc.
BATTERY_KEYS = (
    "AdapterDetails", "Amperage", "AppleRawCurrentCapacity", "AppleRawMaxCapacity",
    "AvgTimeToEmpty", "AvgTimeToFull", "BatteryData", "ChargerData", "CurrentCapacity",
    "CycleCount", "DesignCapacity", "ExternalConnected", "FullyCharged", "InstantAmperage",
    "IsCharging", "MaxCapacity", "PowerTelemetryData", "Temperature", "TimeRemaining",
    "UpdateTime", "Voltage",
)
ADAPTER_KEYS = ("Name", "Description", "Watts", "AdapterVoltage", "Current", "IsWireless")
BATTERY_DATA_KEYS = ("DesignCapacity", "FullChargeCapacity", "RemainingCapacity")
CHARGER_DATA_KEYS = ("NotChargingReason",)
TELEMETRY_KEYS = ("AdapterEfficiencyLoss", "BatteryPower", "SystemLoad", "SystemPowerIn")
NOT_CHARGING_AT_LIMIT = 1 << 24  # NotChargingReason bit powerd sets while the charge limit holds

# Where macOS keeps the charge limit set in System Settings → Battery → Charging.
CHARGING_PREFS = "/Library/Preferences/com.apple.powerd.charging.plist"

# Display backlight power (W). The key follows the chip's tier, not the panel; the first one
# this Mac has with a believable reading wins.
SCREEN_POWER_KEYS = (
    # Pro and Max chips, M1 to M5: measured on this M1 Pro, and by others on the M1 Max
    # (Stats #1718), M3 Max (Stats #1703), M4 Pro (Stats #2552), M5 Pro (Anode) and M5 Max
    # (macpow #12).
    "PDBR",
    # Base chips: measured on the base M4 (Mac16,1: 0.3 W at one bar, 7.2 W at full); in
    # dumps from a base M2 and a base M3 (SMCKit #48, a key list for Mac15,3).
    "PBLR",
    # The A18 Pro MacBook Neo (Mac17,5), measured (macpow #12, Stats #3519). Not the M5:
    # the M5 Max has no PBwo.
    "PBwo",
)
# Above this it isn't a screen. The most anyone has measured is 11 W, an M3 Max at full
# brightness; HDR content goes brighter still.
SCREEN_MAX_W = 25.0
# Macs a sensor report showed have no screen power key at all, by model, so Settings
# doesn't ask them for a report. None yet: the M1 Air (MacBookAir10,1) may be the first.
NO_SCREEN_SENSOR = frozenset()
SMC_KEYS = (
    "PDTR",  # power delivered from the adapter into the system (W)
    "PSTR",  # total system power (W)
    "PPBR",  # battery discharge power (W)
    *SCREEN_POWER_KEYS,
    "PHPC",  # chip power (W): the CPU/GPU package on the heatpipe-cooled rail (not fans)
    "TB0T",  # battery temperature (°C)
    "CHCC",  # charging flag — unreliable on macOS 27, only a last resort
    "B0TE",  # minutes to empty
    "B0TF",  # minutes to full
)

TIME_UNKNOWN = 65535           # IOKit/SMC sentinel for "still estimating"
MAX_PLAUSIBLE_MINUTES = 100 * 60
BATTERY_REFRESH_SECONDS = 3.0  # AppleSmartBattery changes slowly; SMC is read every tick
SMC_RETRY_SECONDS = 30.0


# ── IOKit / CoreFoundation via ctypes ─────────────────────────────────────

_iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
_cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
_libc = ctypes.CDLL(None)

_iokit.IOServiceMatching.restype = ctypes.c_void_p
_iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
_iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
_iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
_iokit.IORegistryEntryCreateCFProperty.argtypes = [
    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
]
_iokit.IOObjectRelease.restype = ctypes.c_int
_iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
_iokit.IOServiceOpen.restype = ctypes.c_int
_iokit.IOServiceOpen.argtypes = [
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
]
_iokit.IOServiceClose.restype = ctypes.c_int
_iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
_iokit.IOConnectCallStructMethod.restype = ctypes.c_int
_iokit.IOConnectCallStructMethod.argtypes = [
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
]
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

_K_CFSTRING_ENCODING_UTF8 = 0x08000100
_MAIN_PORT_DEFAULT = 0
_TASK_SELF = ctypes.c_uint32.in_dll(_libc, "mach_task_self_").value

# CFStrings for the keys, created once and kept for the life of the process.
_CF_KEYS = {k: _cf.CFStringCreateWithCString(None, k.encode(), _K_CFSTRING_ENCODING_UTF8)
            for k in BATTERY_KEYS}


def _to_python(value):
    """Convert a bridged Foundation object tree into plain Python values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if hasattr(value, "keys"):
        return {str(k): _to_python(value[k]) for k in value.keys()}
    if isinstance(value, (list, tuple)) or hasattr(value, "count") and hasattr(value, "objectAtIndex_"):
        return [_to_python(v) for v in value]
    if hasattr(value, "bytes") and hasattr(value, "length"):
        return bytes(value)
    return value


def _pick(d, keys):
    return {k: d[k] for k in keys if k in d} if isinstance(d, dict) else None


def read_battery_registry():
    """Return the AppleSmartBattery properties we use, or None on Macs without a battery.

    The caller must hold an autorelease pool (see PowerReader.read) — PyObjC
    bridging autoreleases objects, and a background thread never drains them
    otherwise (measured: ~2.5 KB leaked per call without one).
    """
    matching = _iokit.IOServiceMatching(b"AppleSmartBattery")
    service = _iokit.IOServiceGetMatchingService(_MAIN_PORT_DEFAULT, matching)  # consumes `matching`
    if not service:
        return None
    props = {}
    try:
        for key, cf_key in _CF_KEYS.items():
            ref = _iokit.IORegistryEntryCreateCFProperty(service, cf_key, None, 0)
            if not ref:
                continue
            try:
                props[key] = _to_python(objc.objc_object(c_void_p=ref))
            finally:
                _cf.CFRelease(ref)
    finally:
        _iokit.IOObjectRelease(service)

    # Keep only the nested fields we use, so nothing identifying is ever held.
    props["AdapterDetails"] = _pick(props.get("AdapterDetails"), ADAPTER_KEYS)
    props["BatteryData"] = _pick(props.get("BatteryData"), BATTERY_DATA_KEYS)
    props["ChargerData"] = _pick(props.get("ChargerData"), CHARGER_DATA_KEYS)
    props["PowerTelemetryData"] = _pick(props.get("PowerTelemetryData"), TELEMETRY_KEYS)
    return props


def parse_charge_limit(data):
    """macOS's charge limit (a percentage below 100) from powerd's charging prefs, or None.

    The prefs hold an NSKeyedArchiver list of ChargeCtrlPolicy objects; plistlib reads
    the archive without unarchiving it. Only the limit set in System Settings counts
    (reason "manualChargeLimit"), not Optimized Battery Charging's temporary hold.
    """
    try:
        objects = plistlib.loads(plistlib.loads(data)["policies"])["$objects"]
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("terminated"):
                continue
            reason = obj.get("reason")
            if isinstance(reason, plistlib.UID):
                reason = objects[reason.data]
            limit = obj.get("soclimit")
            if reason == "manualChargeLimit" and type(limit) is int and 50 <= limit < 100:
                return limit
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        pass  # missing, or a format this version doesn't know: show no limit
    return None


# ── SMC ───────────────────────────────────────────────────────────────────

class _SMCKeyInfo(ctypes.Structure):
    _fields_ = [("data_size", ctypes.c_uint32), ("data_type", ctypes.c_uint32),
                ("data_attributes", ctypes.c_uint8)]


class _SMCVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint8), ("minor", ctypes.c_uint8), ("build", ctypes.c_uint8),
                ("reserved", ctypes.c_uint8), ("release", ctypes.c_uint16)]


class _SMCPLimit(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint16), ("length", ctypes.c_uint16),
                ("cpu", ctypes.c_uint32), ("gpu", ctypes.c_uint32), ("mem", ctypes.c_uint32)]


class _SMCKeyData(ctypes.Structure):
    # Mirrors the kernel's SMCKeyData_t; must be exactly 80 bytes.
    _fields_ = [("key", ctypes.c_uint32), ("vers", _SMCVersion), ("plimit", _SMCPLimit),
                ("key_info", _SMCKeyInfo), ("result", ctypes.c_uint8), ("status", ctypes.c_uint8),
                ("data8", ctypes.c_uint8), ("data32", ctypes.c_uint32),
                ("bytes", ctypes.c_uint8 * 32)]


assert ctypes.sizeof(_SMCKeyData) == 80

_SMC_SELECTOR = 2
_SMC_CMD_READ_BYTES = 5
_SMC_CMD_KEY_BY_INDEX = 8
_SMC_CMD_READ_KEYINFO = 9
_K_IO_RETURN_NOT_PRIVILEGED = -0x1FFFFD3F  # 0xE00002C1; dozens of keys refuse even a look (measured)

# SMC fixed-point types: name -> (fraction bits, signed)
_FIXED_POINT = {
    "fp1f": (15, False), "fp2e": (14, False), "fp3d": (13, False), "fp4c": (12, False),
    "fp5b": (11, False), "fp6a": (10, False), "fp79": (9, False), "fp88": (8, False),
    "fpa6": (6, False), "fpc4": (4, False), "fpe2": (2, False),
    "sp1e": (14, True), "sp2d": (13, True), "sp3c": (12, True), "sp4b": (11, True),
    "sp5a": (10, True), "sp69": (9, True), "sp78": (8, True), "sp87": (7, True),
    "sp96": (6, True), "spa5": (5, True), "spb4": (4, True), "spf0": (0, True),
}


def _is_apple_silicon():
    """True on Apple silicon, including an x86_64 build running under Rosetta."""
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    rc = _libc.sysctlbyname(b"hw.optional.arm64", ctypes.byref(value), ctypes.byref(size), None, 0)
    return rc == 0 and value.value == 1


def mac_model():
    """The model identifier, like Mac16,1. It names the kind of Mac, not this one."""
    size = ctypes.c_size_t(64)
    value = ctypes.create_string_buffer(size.value)
    rc = _libc.sysctlbyname(b"hw.model", value, ctypes.byref(size), None, 0)
    return value.value.decode("ascii", "replace") if rc == 0 else "unknown Mac"


def decode_smc_value(data_type, raw, little_endian):
    """Decode raw SMC bytes. Apple silicon SMCs are little-endian, Intel big-endian."""
    order = "<" if little_endian else ">"
    t = data_type.strip()
    try:
        if t == "flt":
            return struct.unpack(order + "f", raw[:4])[0]
        if t in ("ui8", "flag"):
            return raw[0]
        if t == "si8":
            return struct.unpack("b", raw[:1])[0]
        if t == "ui16":
            return struct.unpack(order + "H", raw[:2])[0]
        if t == "si16":
            return struct.unpack(order + "h", raw[:2])[0]
        if t == "ui32":
            return struct.unpack(order + "I", raw[:4])[0]
        if t == "si32":
            return struct.unpack(order + "i", raw[:4])[0]
        if t in _FIXED_POINT:
            frac, signed = _FIXED_POINT[t]
            # Fixed-point values are big-endian on every SMC that uses them.
            n = struct.unpack(">h" if signed else ">H", raw[:2])[0]
            return n / (1 << frac)
        if t == "ioft":
            # 8 bytes, of which the first 4 are 16.16 fixed point, little-endian. M5 and later
            # use it for temperatures, and so do a few keys on earlier chips (the M1 Pro's TG0B).
            return struct.unpack("<i", raw[:4])[0] / 65536  # signed: a sentinel or a cold day reads below 0
    except struct.error:
        return None
    return None


class SMC:
    """Read-only connection to AppleSMC. Write support is intentionally omitted."""

    def __init__(self):
        matching = _iokit.IOServiceMatching(b"AppleSMC")
        service = _iokit.IOServiceGetMatchingService(_MAIN_PORT_DEFAULT, matching)
        if not service:
            raise OSError("AppleSMC service not found")
        conn = ctypes.c_uint32()
        kr = _iokit.IOServiceOpen(service, _TASK_SELF, 0, ctypes.byref(conn))
        _iokit.IOObjectRelease(service)
        if kr != 0:
            raise OSError(f"IOServiceOpen(AppleSMC) failed: {kr:#x}")
        self._conn = conn.value
        self._key_info = {}
        self._little_endian = _is_apple_silicon()

    def close(self):
        if self._conn:
            _iokit.IOServiceClose(self._conn)
            self._conn = 0

    def __del__(self):
        self.close()

    def _call(self, request):
        response = _SMCKeyData()
        size = ctypes.c_size_t(ctypes.sizeof(_SMCKeyData))
        kr = _iokit.IOConnectCallStructMethod(
            self._conn, _SMC_SELECTOR, ctypes.byref(request), ctypes.sizeof(_SMCKeyData),
            ctypes.byref(response), ctypes.byref(size),
        )
        if kr == _K_IO_RETURN_NOT_PRIVILEGED:
            raise PermissionError(f"SMC refused: {kr:#x}")
        if kr != 0:
            raise OSError(f"SMC call failed: {kr:#x}")
        return response

    def _read_raw(self, key):
        """Return (type, bytes) for a 4-char key, or None if this Mac doesn't have it.

        A key that refuses counts as missing: it says nothing about the connection, and
        raising would lose every other reading until the reconnect.
        """
        code = struct.unpack(">I", key.encode("latin-1"))[0]
        info = self._key_info.get(code)
        try:
            if info is None:
                response = self._call(_SMCKeyData(key=code, data8=_SMC_CMD_READ_KEYINFO))
                if response.result != 0:
                    self._key_info[code] = False
                    return None
                info = (response.key_info.data_size, struct.pack(">I", response.key_info.data_type).decode("latin-1"))
                self._key_info[code] = info
            if info is False:
                return None
            request = _SMCKeyData(key=code, data8=_SMC_CMD_READ_BYTES)
            request.key_info.data_size = info[0]
            response = self._call(request)
        except PermissionError:
            self._key_info[code] = False
            return None
        if response.result != 0:
            return None
        return info[1], bytes(response.bytes[: info[0]])

    def read(self, key):
        """Return the decoded value of a 4-char key, or None if this Mac doesn't have it."""
        found = self._read_raw(key)
        return decode_smc_value(found[0], found[1], self._little_endian) if found else None

    def key_names(self):
        """The name of every key this SMC has (2,181 on an M1 Pro, listed in 15 ms). Names
        only: nothing is read."""
        found = self._read_raw("#KEY")
        count = struct.unpack(">I", found[1][:4])[0] if found else 0  # big-endian even on Apple silicon
        names = []
        for index in range(count):
            response = self._call(_SMCKeyData(data8=_SMC_CMD_KEY_BY_INDEX, data32=index))
            if response.result == 0:
                names.append(struct.pack(">I", response.key).decode("latin-1"))
        return names

    def power_keys(self):
        """Every key starting with P as "NAME=value", or "NAME(type)=?" if it can't be decoded.

        For the log. Other keys are named by the walk but never read, so nothing
        identifying can end up in it.
        """
        keys = []
        for name in self.key_names():
            if not name.startswith("P"):
                continue
            found = self._read_raw(name)
            value = decode_smc_value(found[0], found[1], self._little_endian) if found else None
            if value is not None:
                keys.append(f"{name}={value:.2f}")
            else:
                keys.append(f"{name}({found[0].strip() if found else 'refused'})=?")
        return keys

    def read_all(self, keys=SMC_KEYS):
        values = {}
        for key in keys:
            value = self.read(key)
            if value is not None:
                values[key] = float(value)
        return values


# ── Normalization (pure; unit-tested with captured registry dicts) ────────

def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int) and value >= 2**63:
        value -= 2**64  # negative currents sometimes surface as unsigned 64-bit
    return value


def _positive(value):
    value = _num(value)
    return value if value is not None and value > 0 else None


def _valid_minutes(value):
    value = _num(value)
    if value is None or value <= 0 or value >= TIME_UNKNOWN or value > MAX_PLAUSIBLE_MINUTES:
        return None
    return int(round(value))


def _watts(milliwatts):
    value = _num(milliwatts)
    return value / 1000.0 if value is not None else None


def _round(value, digits=2):
    return round(value, digits) if isinstance(value, (int, float)) else value


def _is_charging(b, smc):
    if b.get("FullyCharged") is True:
        # macOS 27 keeps SMC CHCC at 1 while resting on the adapter at 100%.
        return False
    if b.get("ExternalConnected") is False:
        return False
    amps = _num(b.get("InstantAmperage"))
    if not amps:
        amps = _num(b.get("Amperage"))
    if amps:
        return amps > 0
    if isinstance(b.get("IsCharging"), bool):
        return b["IsCharging"]
    return smc.get("CHCC", 0) > 0


def adapter_name(name, watts):
    """What to call a charger. Apple's name its wattage ("140W USB-C Power Adapter"); others
    only say "pd charger", so they're named the same way from the wattage macOS reports for
    them. None if there's neither a name nor a wattage."""
    name = (name or "").strip()
    if re.search(r"\d\s?W\b", name) or not _positive(watts):
        return name or None
    return f"{round(watts)}W USB-C Power Adapter"


def screen_sensor_unknown(sample, model):
    """True on a laptop that has given no screen reading and isn't known to have no sensor:
    a screen key the app doesn't know yet, so a sensor report would help."""
    return bool(sample and sample.get("has_battery") and not sample.get("has_screen_power")
                and model not in NO_SCREEN_SENSOR)


def missing_smc_keys(smc):
    """The keys in SMC_KEYS that gave no value. Any one screen key is enough."""
    has_screen = any(k in smc for k in SCREEN_POWER_KEYS)
    return [k for k in SMC_KEYS if k not in smc and not (has_screen and k in SCREEN_POWER_KEYS)]


def normalize(battery, smc, now=None, charge_limit=None):
    """Merge a battery registry dict and SMC values into one sample.

    Power values are watts. `battery_power` is signed: positive while charging,
    negative while the battery is powering the Mac. Missing data is None.
    `charge_limit` is macOS's charge limit (see parse_charge_limit), or None.
    """
    now = time.time() if now is None else now
    b = battery or {}
    bd = b.get("BatteryData") or {}
    ptd = b.get("PowerTelemetryData") or {}
    has_battery = battery is not None

    if isinstance(b.get("ExternalConnected"), bool):
        external = b["ExternalConnected"]
    else:
        external = smc.get("PDTR", 0) > 1.0
    charging = _is_charging(b, smc) if has_battery else False
    fully_charged = bool(b.get("FullyCharged")) if has_battery else False

    # Power flow. SMC is live; the registry telemetry is a slower fallback.
    system_load = smc.get("PSTR", _watts(ptd.get("SystemLoad")))
    system_in = smc.get("PDTR", _watts(ptd.get("SystemPowerIn")))
    if not external:
        system_in = 0.0
    efficiency_loss = (_watts(ptd.get("AdapterEfficiencyLoss")) or 0.0) if external else 0.0

    battery_power = None
    if has_battery:
        if system_in is not None and system_load is not None:
            battery_power = system_in - system_load
            if not external and smc.get("PPBR", 0) > 0:
                battery_power = -smc["PPBR"]
        else:
            amps, volts = _num(b.get("InstantAmperage")), _num(b.get("Voltage"))
            if amps is not None and volts is not None:
                battery_power = amps * volts / 1e6
        if battery_power is not None and abs(battery_power) < 0.05:
            battery_power = 0.0

    # Charge level. CurrentCapacity/MaxCapacity is a percentage on Apple silicon
    # and mAh/mAh on older Intel Macs — the ratio is right for both.
    current, maximum = _num(b.get("CurrentCapacity")), _positive(b.get("MaxCapacity"))
    level = int(round(current / maximum * 100)) if current is not None and maximum else None
    if level is not None:
        level = max(0, min(100, level))

    design_mah = _positive(bd.get("DesignCapacity")) or _positive(b.get("DesignCapacity"))
    full_mah = _positive(bd.get("FullChargeCapacity")) or _positive(b.get("AppleRawMaxCapacity"))
    remaining_mah = _positive(bd.get("RemainingCapacity")) or _positive(b.get("AppleRawCurrentCapacity"))
    health = full_mah / design_mah * 100 if full_mah and design_mah else None

    temperature = smc.get("TB0T")
    if temperature is None or not 0 < temperature < 110:
        raw_temp = _num(b.get("Temperature"))  # pre-27 registry: centi-degrees
        temperature = raw_temp / 100 if raw_temp else None

    limit = charge_limit if has_battery and charge_limit else None
    reason = _num((b.get("ChargerData") or {}).get("NotChargingReason")) or 0
    # powerd sets this the moment the limit is reached, while the current is still
    # winding down and IsCharging lags behind.
    at_limit = bool(limit and external and reason & NOT_CHARGING_AT_LIMIT)
    if at_limit:
        charging = False

    minutes = None
    if has_battery and not fully_charged and charging and limit:
        # macOS's own estimates count to 100% even when the limit stops charging
        # sooner. Below about 80% a battery charges at a steady current, so the
        # charge still to go over the averaged current is close; above that the
        # current tapers, and re-estimating every sample lengthens it as it does.
        charge_ma = _positive(b.get("Amperage")) or _positive(b.get("InstantAmperage"))
        if full_mah and charge_ma and level is not None:
            minutes = _valid_minutes(max(limit - level, 1) / 100 * full_mah / charge_ma * 60)
    elif has_battery and not fully_charged and (charging or not external):
        candidates = [b.get("TimeRemaining")]
        if charging:
            candidates += [b.get("AvgTimeToFull"), smc.get("B0TF")]
        else:
            candidates += [b.get("AvgTimeToEmpty"), smc.get("B0TE")]
        minutes = next((m for m in map(_valid_minutes, candidates) if m is not None), None)

    adapter = None
    ad = b.get("AdapterDetails") or {}
    if external and ad:
        watts = _positive(ad.get("Watts"))
        adapter = {
            "name": adapter_name(ad.get("Name") or ad.get("Description"), watts),
            "watts": watts,
            "voltage": _round((_num(ad.get("AdapterVoltage")) or 0) / 1000) or None,
            "current": _round((_num(ad.get("Current")) or 0) / 1000) or None,
            "wireless": bool(ad.get("IsWireless")),
        }

    amps = _num(b.get("InstantAmperage"))
    volts = _num(b.get("Voltage"))
    screen_power = next((smc[k] for k in SCREEN_POWER_KEYS if 0 <= smc.get(k, -1) <= SCREEN_MAX_W), None)
    return {
        "t": now,
        "has_battery": has_battery,
        "has_smc": bool(smc),
        "is_charging": charging,
        "fully_charged": fully_charged,
        "external_connected": external,
        "battery_level": level,
        "charge_limit": limit,
        "at_charge_limit": at_limit,
        "time_remaining_min": minutes,
        "system_load": _round(system_load),
        "system_in": _round(system_in),
        "battery_power": _round(battery_power),
        "adapter_power": _round((system_in or 0) + efficiency_loss) if external else 0.0,
        "efficiency_loss": _round(efficiency_loss),
        "screen_power": _round(screen_power),
        "has_screen_power": screen_power is not None,
        "chip_power": _round(smc.get("PHPC")),
        "temperature": _round(temperature, 1),
        "cycle_count": _num(b.get("CycleCount")),
        "design_capacity": design_mah,
        "max_capacity": full_mah,
        "current_capacity": remaining_mah,
        "health_pct": _round(health, 1),
        "voltage": _round(volts / 1000) if volts else None,
        "amperage": _round(amps / 1000) if amps is not None else None,
        "adapter": adapter,
    }


class PowerReader:
    """Owns the SMC connection and battery-registry cache; call read() once per tick."""

    def __init__(self):
        self._smc = None
        self._smc_retry_at = 0.0
        self._battery = None
        self._battery_read_at = 0.0
        self._last_external = None
        self._charge_limit = None
        self._charging_prefs_mtime = None
        self._checked_smc_keys = False
        self._has_screen_power = False
        self._open_smc()

    def _open_smc(self):
        try:
            self._smc = SMC()
            log.info("AppleSMC connected")
        except OSError as e:
            self._smc = None
            self._smc_retry_at = time.monotonic() + SMC_RETRY_SECONDS
            log.warning("AppleSMC unavailable (%s); using IORegistry telemetry only", e)

    def _read_smc(self):
        if self._smc is None and time.monotonic() >= self._smc_retry_at:
            self._open_smc()
        if self._smc is None:
            return {}
        try:
            return self._smc.read_all()
        except OSError as e:
            log.warning("SMC read failed (%s); reconnecting", e)
            self._smc.close()
            self._smc = None
            self._smc_retry_at = time.monotonic() + SMC_RETRY_SECONDS
            return {}

    def _log_missing_smc_keys(self, smc):
        """Once per launch, name any key this Mac can't read, plus the power keys it does
        have. A new Mac's screen sensor can be found from that without any other tools."""
        self._checked_smc_keys = True
        missing = missing_smc_keys(smc)
        if not missing:
            return
        try:
            keys = " ".join(self._smc.power_keys())
        except OSError as e:
            keys = f"unavailable ({e})"
        log.info("SMC keys missing on %s, macOS %s: %s. Power keys here: %s",
                 mac_model(), platform.mac_ver()[0], ", ".join(missing), keys)

    def _read_charge_limit(self):
        """Re-read powerd's charging prefs only when the file has changed."""
        try:
            mtime = os.stat(CHARGING_PREFS).st_mtime_ns
        except OSError:
            mtime = None
        if mtime == self._charging_prefs_mtime:
            return
        self._charging_prefs_mtime = mtime
        limit = None
        if mtime is not None:
            try:
                with open(CHARGING_PREFS, "rb") as f:
                    limit = parse_charge_limit(f.read())
            except Exception:
                log.exception("Reading the charge limit failed")
        if limit != self._charge_limit:
            log.info("Charge limit: %s", f"{limit}%" if limit else "none")
            self._charge_limit = limit

    def read(self):
        with objc.autorelease_pool():
            smc = self._read_smc()
            if smc and not self._checked_smc_keys:
                self._log_missing_smc_keys(smc)
            # Plugging in or unplugging shows up in SMC instantly; refresh the
            # registry right away so charge state doesn't lag the power numbers.
            external_hint = smc.get("PDTR", 0) > 1.0 if "PDTR" in smc else None
            stale = time.monotonic() - self._battery_read_at >= BATTERY_REFRESH_SECONDS
            if stale or (external_hint is not None and external_hint != self._last_external):
                try:
                    self._battery = read_battery_registry()
                except Exception:
                    log.exception("Reading AppleSmartBattery failed")
                self._read_charge_limit()
                self._battery_read_at = time.monotonic()
                self._last_external = external_hint
            sample = normalize(self._battery, smc, charge_limit=self._charge_limit)
            # Once seen, it stays: a failed read or an SMC reconnect shouldn't hide Screen.
            self._has_screen_power = self._has_screen_power or sample["has_screen_power"]
            sample["has_screen_power"] = self._has_screen_power
            return sample
