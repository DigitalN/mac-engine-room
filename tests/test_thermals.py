"""Which SMC keys count as CPU, GPU, memory, SSD and ambient, against keys captured on a
real Mac, plus what the reader reads and when."""

import math
from pathlib import Path

import pytest

import power
import report
import thermals
from thermals import ALL_PARTS, TEMPERATURES, core_table, normalize, sensor_keys

# This M1 Pro (MacBookPro18,1, macOS 27.2), lightly loaded, 2026-11-02: every Tp, Te, TC,
# Tg, TG, Tm, TH, TA and Ta key, the battery's TB0T and the fans. TG… are ioft, the rest flt.
M1_PRO = dict((k, float(v)) for k, v in (pair.split("=") for pair in """
F0Ac=0.00 F0Md=3 F0Mn=1499.00 F0Mx=4296.00 F1Ac=0.00 F1Md=3 F1Mn=1499.00 F1Mx=4744.00 FNum=2
TAOL=27.00 TB0T=31.30 TC10=54.68 TC11=56.67 TC12=55.48 TC13=55.68 TC20=52.72 TC21=52.96 TC22=52.81
TC23=52.88 TC30=54.01 TC31=55.45 TC32=55.14 TC33=55.09 TCDX=56.69 TCHP=44.18 TCMb=63.63 TCMz=69.58
TG0B=31.30 TG0C=31.00 TG0H=31.00 TG0V=31.30 TG1B=31.00 TG2B=31.30 TH0a=37.68 TH0b=37.04 TH0x=37.68
TH1a=0.00 TH1b=0.00 Ta00=0.00 Ta01=0.00 Ta02=0.00 Ta04=0.00 Ta05=8.49 Ta06=10.90 TaLP=47.01
TaLT=41.49 TaLW=36.98 TaRF=49.01 TaRT=35.23 TaRW=35.81 TaTP=49.67 Te00=49.73 Te01=63.09 Te02=68.70
Tg04=52.59 Tg05=61.79 Tg0C=51.19 Tg0D=60.39 Tm00=53.11 Tm01=53.11 Tm02=54.09 Tm04=52.51 Tm05=52.51
Tm06=53.45 Tp00=54.63 Tp01=63.63 Tp02=68.83 Tp04=52.58 Tp05=61.56 Tp06=67.81 Tp08=52.40 Tp09=61.37
Tp0A=65.55 Tp0C=50.63 Tp0D=59.78 Tp0E=67.08 Tp0G=52.05 Tp0H=61.16 Tp0I=69.35 Tp0K=50.89 Tp0L=59.73
Tp0M=66.84 Tp0O=52.77 Tp0P=61.66 Tp0Q=69.58 Tp0S=53.15 Tp0T=62.21 Tp0U=66.94 Tp0W=52.10 Tp0X=61.03
Tp0Y=67.54 Tp0a=50.50 Tp0b=59.49 Tp0c=66.65""".split()))


def test_m1_pro_reads_its_core_keys_and_nothing_else():
    keys = sensor_keys(M1_PRO, "Apple M1 Pro", fan_count=2)
    # The middle key of each Tp triple; never the hotspots, the helpers or the battery.
    assert keys["cpu"] == ("Tp09", "Tp0T", "Tp01", "Tp05", "Tp0D", "Tp0H", "Tp0L", "Tp0P", "Tp0X", "Tp0b")
    assert keys["gpu"] == ("Tg05", "Tg0D")
    assert keys["memory"] == ("Tm00", "Tm01", "Tm02", "Tm04", "Tm05", "Tm06")
    assert keys["ssd"] == ("TH0a", "TH0b", "TH0x", "TH1a", "TH1b")
    assert keys["ambient"] == ("TAOL",)
    assert keys["fans"] == (("F0Ac", "F0Mn", "F0Mx", "F0Md"), ("F1Ac", "F1Mn", "F1Mx", "F1Md"))
    every = {k for group in TEMPERATURES for k in keys[group]}
    assert not every & {"TCDX", "TCMb", "TCMz", "Tp02", "Tp0Q", "TG0B", "TB0T", "Ta05"}


def test_m1_pro_sample():
    s = normalize(M1_PRO, sensor_keys(M1_PRO, "Apple M1 Pro", fan_count=2), now=1.0)
    assert (s["cpu"], s["gpu"], s["memory"], s["ssd"], s["ambient"]) == (63.6, 61.8, 54.1, 37.7, 27.0)
    assert s["hottest"] == 63.6  # not TCMz's 69.6
    assert s["sensors"] == {"cpu": 10, "gpu": 2, "memory": 6, "ssd": 3, "ambient": 1}  # TH1a/TH1b read 0
    assert s["fans"] == [{"rpm": 0, "min": 1499, "max": 4296, "manual": False},
                         {"rpm": 0, "min": 1499, "max": 4744, "manual": False}]


def test_readings_of_exactly_0_or_outside_minus_40_to_130_are_ignored():
    keys = {"cpu": ("Tp01", "Tp05", "Tp09", "Tp0D", "Tp0H"), "ssd": ("TH0a",)}
    s = normalize({"Tp01": 0.0, "Tp05": 131.0, "Tp09": math.nan, "Tp0D": -45.0, "Tp0H": 55.5, "TH0a": 0.0}, keys)
    assert s["cpu"] == 55.5 and s["sensors"]["cpu"] == 1
    assert s["ssd"] is None and s["hottest"] == 55.5
    assert normalize({}, keys)["hottest"] is None


def test_a_cold_day_outside_still_reads():
    keys = {"cpu": ("Tp01",), "gpu": ("Tg05",), "ambient": ("TAOL",), "ssd": ("TH0a",)}
    s = normalize({"Tp01": 12.0, "Tg05": 6.5, "TAOL": -5.25, "TH0a": 3.0}, keys)
    assert (s["ambient"], s["ssd"], s["cpu"], s["gpu"]) == (-5.2, 3.0, 12.0, 6.5)  # nothing hidden for being cold


def test_parked_chip_sensors_are_ignored():
    keys = {"cpu": ("Tp01", "Tp05"), "gpu": ("Tg05", "Tg0D", "Tg0L"), "ambient": ("TAOL",), "memory": ("Tm00",)}
    room = {"TAOL": 27.0, "Tm00": 45.0}
    # A powered-down GPU's made-up 1.5–5° readings, well under the Mac's coolest other sensor.
    s = normalize({**room, "Tp01": 48.0, "Tp05": 3.1, "Tg05": 2.4, "Tg0D": 5.2, "Tg0L": -4.0}, keys)
    assert s["cpu"] == 48.0 and s["sensors"]["cpu"] == 1 and s["gpu"] is None and s["sensors"]["gpu"] == 0
    # One value on every key (an M4 Pro's flat 40.0) is made up too.
    assert normalize({**room, "Tp01": 48.0, "Tg05": 40.0, "Tg0D": 40.0, "Tg0L": 40.0}, keys)["gpu"] is None
    # An idle GPU that's awake counts, however hot the CPU is.
    s = normalize({**room, "Tp01": 98.0, "Tp05": 97.0, "Tg05": 44.0, "Tg0D": 45.5, "Tg0L": 43.0}, keys)
    assert s["gpu"] == 45.5 and s["sensors"]["gpu"] == 3
    # Without another sensor to compare with, nothing is called parked.
    assert normalize({"Tg05": 3.0}, {"gpu": ("Tg05",)})["gpu"] == 3.0


def test_core_table_picks_the_variant_then_the_generation():
    cpu, gpu = core_table("Apple M4 Pro")
    assert "Tg1U" in gpu and "Tg0G" not in gpu
    assert "Tg0G" in core_table("Apple M4")[1] and "Tg1U" not in core_table("Apple M4")[1]
    assert core_table("Apple M1 Max") == core_table("Apple M1")
    assert core_table("Apple M5 Ultra") == core_table("Apple M5")
    assert core_table("Apple M3")[0][0] == "Te05" and core_table("Apple A18 Pro")[1] == ("Tg05", "Tg0D", "Tg0L", "Tg0e")
    assert core_table("Apple M7") is None and core_table("Intel(R) Core(TM) i9") is None and core_table("") is None


def test_a_chip_the_table_doesnt_know_reads_every_core_prefix_but_never_the_battery():
    keys = sensor_keys(M1_PRO, "Apple M7 Max")
    assert keys["cpu"] == tuple(sorted(k for k in M1_PRO if k[:2] in ("Tp", "Te")))
    assert keys["gpu"] == ("Tg04", "Tg05", "Tg0C", "Tg0D")
    s = normalize(M1_PRO, keys)
    assert s["cpu"] == 69.6 and s["gpu"] == 61.8  # helpers count: too warm, never too cool


def test_a_known_chip_without_its_table_keys_falls_back_too():
    names = {"Tp00": 50.0, "Tp02": 60.0, "Tg0Z": 40.0}  # an M2 with keys Stats doesn't list
    keys = sensor_keys(names, "Apple M2 Pro")
    assert keys["cpu"] == ("Tp00", "Tp02") and keys["gpu"] == ("Tg0Z",)
    assert keys["by_prefix"] == ("cpu", "gpu")


def test_a_table_with_too_few_keys_for_the_cores_falls_back():
    assert sensor_keys(M1_PRO, "Apple M1 Pro", cores=10)["by_prefix"] == ()
    keys = sensor_keys(M1_PRO, "Apple M1 Pro", cores=24)  # 10 core keys on a 24-core chip: not this table
    assert keys["by_prefix"] == ("cpu",) and len(keys["cpu"]) > 10 and keys["gpu"] == ("Tg05", "Tg0D")


def test_the_m3_reads_its_cores_not_the_gpu_fabric():
    # A base M3's cores (iSMC's dumps): no Tf keys at all (Stats #1703).
    names = "Te04 Te05 Te06 Te0G Te0H Te0I Te0P Te0Q Te0R Te0S Te0T Tp04 Tp05 Tp06 Tp0C Tp0D Tp0E Tp0K Tp0L Tp0M " \
            "Tp0a Tp0b Tp0c Tg04 Tg05 Tg0C Tg0D Tf0D".split()
    keys = sensor_keys(names, "Apple M3", cores=8)
    assert keys["cpu"] == ("Te05", "Te0H", "Te0P", "Te0S", "Tp05", "Tp0D", "Tp0L", "Tp0b") and keys["by_prefix"] == ()
    assert keys["gpu"] == ("Tg05", "Tg0D")


def test_the_base_m1_reads_its_b_keys():
    _, values = report.parse((Path(__file__).parent / "sensor_reports" / "MacBookAir10,1.txt").read_text())
    keys = sensor_keys({k for k, v in values.items() if v is not None}, "Apple M1", cores=8)
    assert keys["cpu"] == ("Te0b", "Te3b", "Tp2b", "Tp3b", "Tp4b", "Tp5b", "Tp7b", "Tp8b", "Tp9b")
    assert keys["gpu"] == ("Tg1b", "Tg4b") and keys["by_prefix"] == ()
    s = normalize(values, keys)
    assert s["cpu"] == round(values["TCMb"], 1)  # the chip's own "hottest core", not TCMz's 41.7
    assert s["gpu"] is None and s["memory"] is None and s["ambient"] is None  # idle GPU reads 0; no Tm or TA keys


def test_both_m4_pro_core_layouts():
    a = "Te05 Te0S Te06 Te0T Tp01 Tp05 Tp09 Tp0D Tp0H Tp0L Tp0P Tp0T Tp0X Tp0b Tg1U Tg1k".split()
    b = "Te05 Te0S Te06 Te0T Tp1j Tp1n Tp1t Tp1w Tp1z Tp22 Tp25 Tp28 Tp2B Tp2E Tg1U Tg1k".split()
    for names in (a, b):
        keys = sensor_keys(names, "Apple M4 Pro", cores=14)
        assert set(keys["cpu"]) == set(names) - {"Tg1U", "Tg1k"} and keys["by_prefix"] == ()


def test_the_ultras_second_die():
    cpu, gpu = core_table("Apple M1 Ultra")
    assert {"Tp09", "Tp29", "Tp2b"} <= set(cpu) and {"Tg05", "Tg0z"} <= set(gpu)
    assert "Tp29" not in core_table("Apple M1 Max")[0]


def test_m5_memory_leaves_out_the_keys_that_copy_a_core():
    names = ["Tm00", "Tm04", "Tm0y", "Tm2T"]
    assert sensor_keys(names, "Apple M5 Max")["memory"] == ("Tm00", "Tm04")
    assert sensor_keys(names, "Apple M4")["memory"] == tuple(sorted(names))


def test_fans_are_counted_where_fnum_is_missing():
    assert thermals.count_fans({"F0Ac", "F1Ac", "F0Mn"}) == 2
    assert thermals.count_fans({"FNum", "TAOL"}) == 0


def test_fan_mode_and_the_lowercase_mode_key():
    names = {"F0Ac": 2400.4, "F0Mn": 1200.0, "F0Mx": 6000.0, "F0md": 1.0, "F0Md": 3.0}
    keys = sensor_keys(names, "Apple M5", fan_count=1)
    assert keys["fans"] == (("F0Ac", "F0Mn", "F0Mx", "F0md"),)
    assert normalize(names, keys)["fans"] == [{"rpm": 2400, "min": 1200, "max": 6000, "manual": True}]
    # Fan keys not read this time (or a mode this Mac lacks) don't invent a fan or a mode.
    assert normalize({}, keys)["fans"] == []
    assert normalize({"F0Ac": 1500.0}, keys)["fans"][0]["manual"] is None


def test_a_mac_without_fans_has_none():
    assert sensor_keys(M1_PRO, "Apple M2", fan_count=0)["fans"] == ()
    assert normalize(M1_PRO, sensor_keys(M1_PRO, "Apple M2"))["fans"] == []


class _FakeSMC:
    """Keys map to values; `fail` makes the next read_all raise, as a dropped connection does."""
    opened = 0

    def __init__(self, values):
        _FakeSMC.opened += 1
        self.values, self.listed, self.reads, self.fail = values, 0, [], False

    def key_names(self):
        self.listed += 1
        return list(self.values)

    def read(self, key):
        return self.values.get(key)

    def read_all(self, keys):
        if self.fail:
            raise OSError("SMC call failed")
        self.reads.append(list(keys))
        return {k: self.values[k] for k in keys if k in self.values}

    def close(self):
        pass


@pytest.fixture
def reader(monkeypatch):
    _FakeSMC.opened = 0
    smcs = []
    monkeypatch.setattr(power, "SMC", lambda: smcs.append(_FakeSMC(M1_PRO)) or smcs[-1])
    monkeypatch.setattr(thermals, "chip_name", lambda: "Apple M1 Pro")
    monkeypatch.setattr(thermals, "core_count", lambda: 10)  # the M1 Pro's, whatever runs the tests
    r = thermals.ThermalsReader()
    r.smcs = smcs
    return r


def test_keys_are_found_once_and_only_the_parts_asked_for_are_read(reader):
    s = reader.read({"cpu"})
    smc = reader.smcs[0]
    assert s["cpu"] == 63.6 and s["gpu"] is None and s["fans"] == []
    assert s["available"] == ["cpu", "gpu", "memory", "ssd", "ambient", "fans"]
    assert smc.reads[-1] == list(sensor_keys(M1_PRO, "Apple M1 Pro")["cpu"])

    s = reader.read(ALL_PARTS)
    assert s["hottest"] == 63.6 and len(s["fans"]) == 2 and s["fans"][1]["max"] == 4744
    assert "F0Mn" not in smc.reads[-1]  # fan limits are read once, when the keys are found
    assert smc.listed == 1 and _FakeSMC.opened == 1


def test_a_failed_read_reconnects_and_finds_the_keys_again(reader, monkeypatch):
    reader.read({"cpu"})
    reader.smcs[0].fail = True
    assert reader.read({"cpu"}) is None
    now = [thermals.time.monotonic()]
    monkeypatch.setattr(thermals.time, "monotonic", lambda: now[0])
    assert reader.read({"cpu"}) is None  # waits before trying again
    now[0] += thermals.SMC_RETRY_SECONDS
    assert reader.read({"cpu"})["cpu"] == 63.6
    assert _FakeSMC.opened == 2 and reader.smcs[1].listed == 1


def test_a_parked_gpu_shows_its_last_reading_but_only_for_display(reader):
    first = reader.read({"gpu", "ambient", "memory"})
    assert first["gpu"] == 61.8 and first["gpu_held"] is None
    reader.smcs[0].values = {**M1_PRO, "Tg05": 2.4, "Tg0D": 3.0}  # the GPU powers down
    held = reader.read({"gpu", "ambient", "memory"})
    assert held["gpu"] is None and held["hottest"] != 61.8  # the fan curve and the title never see it
    assert held["gpu_held"] == {"c": 61.8, "t": first["t"], "sensors": 2}


def test_the_reader_says_what_it_guessed(reader, monkeypatch):
    reader.read({"cpu"})
    assert reader.guessing == ()
    monkeypatch.setattr(power, "SMC", lambda: _FakeSMC({k: v for k, v in M1_PRO.items() if not k.startswith("Tg")}))
    other = thermals.ThermalsReader()
    other._chip = "Apple M9"
    other.read({"cpu"})
    assert other.guessing == ("cpu", "gpu")  # CPU by prefix, and no GPU key at all


def test_ioft_is_decoded():
    # TG0B on the M1 Pro: 31.3 °C as 16.16 fixed point, then 4 bytes of padding.
    assert power.decode_smc_value("ioft", bytes.fromhex("cd4c1f0000000000"), little_endian=True) == pytest.approx(31.3, abs=0.001)
    assert power.decode_smc_value("ioft", bytes.fromhex("cd4c1f0000000000"), little_endian=False) == pytest.approx(31.3, abs=0.001)
    assert power.decode_smc_value("ioft", bytes.fromhex("0000fcff00000000"), little_endian=True) == -4.0  # signed


def test_reads_this_mac():
    """Live read through the real SMC (skipped where there's none, or no temperatures)."""
    s = thermals.ThermalsReader().read()
    if s is None or not s["available"]:
        pytest.skip("no SMC temperatures on this machine")
    assert all(g in thermals.ALL_PARTS for g in s["available"])
    for group in TEMPERATURES:
        assert s[group] is None or thermals.MIN_C <= s[group] <= thermals.MAX_C
