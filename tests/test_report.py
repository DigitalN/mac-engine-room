"""The sensor report (report.py), and every report kept in tests/sensor_reports/: the Macs they
came from must keep finding their sensors.

When a report comes in on GitHub, save it there as <model>.txt. Until the tables cover that
Mac, the test fails and says what's missing; a Mac that really has no screen sensor goes in
power.NO_SCREEN_SENSOR, and one with no chip sensor in power.NO_CHIP_SENSOR.
"""

from pathlib import Path

import pytest

import power
import report
import thermals

REPORTS = sorted((Path(__file__).parent / "sensor_reports").glob("*.txt"))
ABOUT = {"app": "1.2.13", "mac": "MacBookPro18,1", "chip": "Apple M1 Pro", "cores": 10, "battery": True, "macos": "27.2"}


def test_a_report_reads_back_as_it_was_made():
    values = {"PSTR": 9.25, "PDBR": 2.5, "TC0 ": 40.0, "Tp09": 48.42, "Tg05": 50.47, "FNum": 2, "F0Ac": 1487.89, "FBAD": None}
    text = report.build(ABOUT, values)
    about, back = report.parse(text)
    assert about == ABOUT
    assert back == {**{k: round(float(v), 2) for k, v in values.items() if v is not None}, "FBAD": None}
    assert "Screen power: PDBR" in text and "GPU: Tg05 (core key table)" in text and "FBAD=?" in text
    assert "Chip power: none (PHPC missing)" in text
    assert "CPU: Tp09 (by prefix)" in text  # one core key on a 10-core chip: the table doesn't fit
    assert "Power keys missing: PDTR, PPBR, PHPC" in text


def test_a_report_says_what_it_had_to_guess():
    text = report.build({**ABOUT, "chip": "Apple M9", "battery": False}, {"Tp01": 50.0, "Te05": 45.0})
    assert "CPU: Te05 Tp01 (by prefix)" in text and "GPU: none found" in text
    assert "Screen power: none of PDBR, PBLR, PBwo" in text and "Battery: no" in text
    assert "Chip power: none (PHPC missing)" in text


def test_a_report_names_the_key_each_power_reading_uses():
    for values, screen, chip in [
        ({"PDBR": 2.5, "PHPC": 1.8}, "PDBR", "PHPC"),
        ({"PBLR": 7.2, "PHPC": 0.1}, "PBLR", "PHPC"),  # 0.1 W counts
        ({"PBLR": 0.01, "PHPC": 0.01}, "none (PBLR reads 0.01 W)", "none (PHPC reads 0.01 W)"),  # the M4 Air
        ({"PBLR": 0.0, "PHPC": 0.53}, "none (PBLR reads 0.00 W)", "PHPC"),  # the M1 Air
        ({"PBLR": 0.09, "PHPC": 0.09}, "none (PBLR reads 0.09 W)", "none (PHPC reads 0.09 W)"),
        ({"PDBR": 0.0, "PBLR": 0.01}, "none (PDBR reads 0.00 W, PBLR reads 0.01 W)", "none (PHPC missing)"),
        ({"PDBR": 60.0, "PBLR": 7.2}, "PBLR", "none (PHPC missing)"),  # no screen reads 60 W
        ({"PDBR": 60.0}, "none (PDBR reads 60.00 W)", "none (PHPC missing)"),
        ({"PHPC": None, "PBLR": None}, "none of PDBR, PBLR, PBwo", "none (PHPC missing)"),
    ]:
        assert report.power_sources(values) == (screen, chip), values
        text = report.build(ABOUT, values)
        assert f"  Screen power: {screen}\n  Chip power: {chip}\n" in text
        about, back = report.parse(text)  # the new lines are for people: reading a report back skips them
        assert about == ABOUT and back == {k: None if v is None else round(v, 2) for k, v in values.items()}


def test_a_report_names_ioreport_where_chip_comes_from_it():
    m4_air = {"PBLR": 0.01, "PHPC": 0.01}
    assert report.power_sources(m4_air, "CLPC energy counters") == (
        "none (PBLR reads 0.01 W)", "IOReport's CLPC energy counters, CPU + GPU + ANE (PHPC reads 0.01 W)")
    assert report.power_sources({}, "Energy Model")[1] == "IOReport's Energy Model, CPU + GPU + ANE (PHPC missing)"
    # Where it was wanted but couldn't be used, why. A working PHPC (the switch) is used instead.
    assert report.power_sources(m4_air, None, "libIOReport won't load")[1] == \
        "none (PHPC reads 0.01 W; IOReport unavailable: libIOReport won't load)"
    assert report.power_sources({"PHPC": 3.2}, None, "libIOReport won't load")[1] == "PHPC"
    about = {**ABOUT, "mac": "Mac16,12", "chip": "Apple M4", "ioreport": "CLPC energy counters", "ioreport_error": None}
    text = report.build(about, m4_air)
    assert "  Chip power: IOReport's CLPC energy counters, CPU + GPU + ANE (PHPC reads 0.01 W)\n" in text
    assert "IOReport counters" not in text  # only once it has some
    back, _ = report.parse(text)  # the source is for people: reading a report back skips it
    assert back == {k: v for k, v in about.items() if not k.startswith("ioreport")}


def test_a_report_says_what_each_ioreport_counter_read():
    # One report is enough to tell a counter that's missing or in a unit the app doesn't know.
    m4_air = {"PBLR": 0.01, "PHPC": 0.01}
    for counters in ("CPU 1.20 W, no GPU counter, ANE 0.00 W", 'CPU 1.20 W, GPU in "xJ", ANE 0.00 W', "no reading yet"):
        about = {**ABOUT, "mac": "Mac16,12", "chip": "Apple M4", "ioreport": "CLPC energy counters",
                 "ioreport_counters": counters}
        text = report.build(about, m4_air)
        assert ("  Chip power: IOReport's CLPC energy counters, CPU + GPU + ANE (PHPC reads 0.01 W)\n"
                f"  IOReport counters: {counters}\n") in text
        back, values = report.parse(text)
        assert back == {k: v for k, v in about.items() if not k.startswith("ioreport")} and values == m4_air
    # And after IOReport gave up, what they read before it did.
    about = {**ABOUT, "ioreport_error": "the CPU counter isn't counting (Energy Model)",
             "ioreport_counters": "CPU 0.00 W, GPU 1.50 W, ANE 0.00 W"}
    text = report.build(about, m4_air)
    assert ("  Chip power: none (PHPC reads 0.01 W; IOReport unavailable: the CPU counter isn't counting (Energy Model))\n"
            "  IOReport counters: CPU 0.00 W, GPU 1.50 W, ANE 0.00 W\n") in text


@pytest.mark.parametrize("path", REPORTS, ids=[p.stem for p in REPORTS])
def test_every_reported_mac_finds_its_sensors(path):
    about, values = report.parse(path.read_text())
    names = {k for k, v in values.items() if v is not None}
    fans = int(values.get("FNum") or 0) or thermals.count_fans(names)
    keys = thermals.sensor_keys(names, about["chip"], fans, about["cores"])
    assert keys["by_prefix"] == (), f"{about['chip']} isn't in the core key table: {keys['by_prefix']} by prefix"
    assert keys["cpu"] and keys["gpu"]
    if about["battery"]:
        screen = [values[k] for k in power.SCREEN_POWER_KEYS if k in names]
        if about["mac"] not in power.NO_SCREEN_SENSOR:
            assert any(power.sensor_works(w) for w in screen), \
                f"no screen key reads at least {power.SENSOR_MIN_W} W on {about['mac']}: {screen}"
        if about["mac"] not in power.NO_CHIP_SENSOR:
            assert power.sensor_works(values.get("PHPC")), \
                f"PHPC doesn't read at least {power.SENSOR_MIN_W} W on {about['mac']}: {values.get('PHPC')}"


def test_this_macs_report_has_nothing_but_power_temperature_and_fan_keys():
    _, values = report.parse(REPORTS[0].read_text())
    assert values and all(k[0] in report.PREFIXES for k in values)
