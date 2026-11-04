"""The sensor report that Settings › About copies, for a GitHub issue about a wrong or
missing reading.

Plain text, so it pastes anywhere. parse() reads one back for the tests: each report that
comes in is kept in tests/sensor_reports/, so the Mac it came from stays right.

It names the kind of Mac (model, chip, cores, macOS) and lists the SMC's power,
temperature and fan keys (P…, T…, F…) with their values. Nothing identifying: no serial
number or computer name. Only numbers are shown; a key that isn't a number shows as "?".
Walking the keys takes a moment (about 500 reads), so it's only done when asked.
"""

import platform
import re

import objc

import power
import thermals

TITLE = "Mac Engine Room sensor report"
PREFIXES = ("P", "T", "F")
POWER_KEYS = ("PSTR", "PDTR", "PPBR", "PHPC")
PER_LINE = 8
_PAIR = re.compile(r"(?:^|  )(.{4})=(\S+)")  # SMC keys are four characters, a space included


def collect(version, sample=None):
    """The report for this Mac, from its own SMC connection. Raises OSError without one."""
    with objc.autorelease_pool():
        smc = power.SMC()
        try:
            values = {name: smc.read(name) for name in smc.key_names() if name.startswith(PREFIXES)}
        finally:
            smc.close()
    about = {"app": version, "mac": power.mac_model(), "chip": thermals.chip_name(),
             "cores": thermals.core_count(), "battery": sample.get("has_battery") if sample else None,
             "macos": platform.mac_ver()[0]}
    return build(about, values)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build(about, values):
    """The report's text. Pure: `about` as collect() makes it, `values` {key: number or None}."""
    names = {k for k, v in values.items() if v is not None}
    fan_count = int(values.get("FNum") or 0) or thermals.count_fans(names)
    keys = thermals.sensor_keys(names, about.get("chip"), fan_count, about.get("cores") or 0)
    screen = next((k for k in power.SCREEN_POWER_KEYS
                   if _number(values.get(k)) and 0 <= values[k] <= power.SCREEN_MAX_W), None)
    battery = {True: "yes", False: "no"}.get(about.get("battery"), "unknown")

    def source(group):
        listed = " ".join(keys[group]) or "none found"
        if group in thermals.CHIP_GROUPS and keys[group]:
            return f"{listed} ({'by prefix' if group in keys['by_prefix'] else 'core key table'})"
        return listed

    missing = [k for k in POWER_KEYS if not _number(values.get(k))]
    lines = [
        TITLE, "",
        f"App: {about.get('app')}", f"Mac: {about.get('mac')}", f"Chip: {about.get('chip') or 'unknown'}",
        f"Cores: {about.get('cores') or 'unknown'}", f"Battery: {battery}", f"macOS: {about.get('macos')}", "",
        "Where each reading comes from",
        f"  Screen power: {screen or 'none of ' + ', '.join(power.SCREEN_POWER_KEYS)}",
        f"  Power keys missing: {', '.join(missing) or 'none'}",
        *(f"  {label}: {source(group)}" for group, label in
          (("cpu", "CPU"), ("gpu", "GPU"), ("memory", "Memory"), ("ssd", "SSD"), ("ambient", "Ambient"))),
        f"  Fans: {fan_count}" + (f" ({', '.join(fan[3] for fan in keys['fans'])})" if fan_count else ""),
        "", "Keys",
    ]
    pairs = [f"{k}={values[k]:.2f}" if _number(values[k]) else f"{k}=?" for k in sorted(values)]
    lines += ["  ".join(pairs[i:i + PER_LINE]) for i in range(0, len(pairs), PER_LINE)]
    return "\n".join(lines) + "\n"


def parse(text):
    """(about, values) from a report's text, as build() was given them ("?" values are None)."""
    head, _, keys = text.partition("\nKeys\n")
    fields = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line and not line.startswith(" "))
    cores = fields.get("Cores", "")
    about = {"app": fields.get("App"), "mac": fields.get("Mac"), "chip": fields.get("Chip"),
             "cores": int(cores) if cores.isdigit() else 0,
             "battery": {"yes": True, "no": False}.get(fields.get("Battery")), "macos": fields.get("macOS")}
    values = {}
    for line in keys.splitlines():
        for key, value in _PAIR.findall(line):
            values[key] = None if value == "?" else float(value)
    return about, values
