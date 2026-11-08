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
