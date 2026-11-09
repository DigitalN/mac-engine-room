"""ioreport.py: which channels count as the chip, the watts from their counters, and when a
reading can't be trusted. Plus a live read of this Mac's IOReport."""

import re
import time

import objc
import pytest

import ioreport
from ioreport import (chip_watts, clpc_channels, counter_deltas, describe_readings, energy_model_part,
                      part_readings, reading_problems)

# The Energy Model's channels on the M1 Pro (macOS 27.2), abridged: per-core and per-cluster CPU
# channels, the totals, and the rest of the chip.
M1_PRO_ENERGY_MODEL = [
    "EACC_CPU0", "PACC0_CPU3", "PACC1_CPU0", "EACC_CPM", "PACC0_CPM", "EACC_CPU", "PACC0_CPU", "PACC1_CPU",
    "CPU Energy", "ECPUDTL00", "PCPUDTL3e", "PCPU1DTL00", "GPU0", "GPU SRAM0", "ANE0", "ISP0", "AVE0", "MSR0",
    "DCS0", "DRAM0", "AMCC0", "PCIe Port 0 Energy", "apciec0 Energy", "GPU Energy",
]


def test_the_energy_model_counts_each_part_once():
    parts = {name: energy_model_part(name) for name in M1_PRO_ENERGY_MODEL if energy_model_part(name)}
    assert parts == {"CPU Energy": "cpu", "GPU Energy": "gpu", "ANE0": "ane"}
    # Other chips name them differently (macmon): an Ultra has a CPU total per die, and ANEs are numbered.
    assert [energy_model_part(n) for n in ("DIE_0_CPU Energy", "DIE_1_CPU Energy", "ANE", "ANE0_1")] == \
        ["cpu", "cpu", "ane", "ane"]


def test_clpc_report_ids_put_the_table_index_above_the_drivers_key():
    # Full IDs from macmon's own test, from the macOS 27.0.1 restore image: M1 Ultra, M2, M5.
    for bundle, ids in [
        ("com.apple.driver.AppleT6002CLPC", [0x0000_0010_B501_816B, 0x0000_0019_9E2C_3E8B, 0x0000_0018_14D5_A574]),
        ("com.apple.driver.AppleT8112CLPC", [0x0000_0010_B543_5137, 0x0000_0019_638D_9D52, 0x0000_0018_EA08_9C36]),
        ("com.apple.driver.AppleT8142CLPC", [0x0000_0010_4370_8744, 0x0000_0019_6A01_0933, 0x0000_0018_BC5E_FDF0]),
    ]:
        for version in ("27.0", "27.0.1", "27.2"):
            channels = clpc_channels(bundle, version)
            assert [c[0] for c in channels] == ids
            assert [c[1:] for c in channels] == [("CPU Energy", "cpu"), ("GPU Energy", "gpu"), ("ANE", "ane")]
    # The M4, as in the 13" M4 Air.
    assert clpc_channels("com.apple.driver.AppleT8132CLPC", "27.2")[0][0] == 0x0000_0010_4A85_4D94


def test_clpc_counters_are_only_known_on_macos_27():
    for version in ("", None, "15.8", "26.6.2", "28.0", "127.0"):
        assert clpc_channels("com.apple.driver.AppleT8132CLPC", version) is None
    assert clpc_channels("com.apple.driver.AppleUnknownCLPC", "27.2") is None
    assert clpc_channels("AppleT8132CLPC", "27.2") is None


def test_each_driver_has_three_different_counters():
    for keys in ioreport.CLPC_REPORTS.values():
        assert len(keys) == 3 and len(set(keys)) == 3


def test_watts_take_each_channels_own_unit():
    # One second of the M1 Pro's CLPC counters, in nJ: CPU 2.12 W, GPU 1.50 W, the Neural Engine idle.
    assert chip_watts([("cpu", 2_120_000_000, "nJ"), ("gpu", 1_500_000_000, "nJ"), ("ane", 0, "nJ")], 1.0) == \
        pytest.approx(3.62)
    # The Energy Model mixes them: CPU and ANE in mJ, GPU in nJ. Over 2.5 s.
    assert chip_watts([("cpu", 5000, "mJ"), ("gpu", 2_500_000_000, "nJ"), ("ane", 250, "mJ")], 2.5) == \
        pytest.approx(3.1)
    assert chip_watts([("cpu", 1_000_000, "uJ"), ("gpu", 1_000_000, "µJ")], 1.0) == pytest.approx(2.0)
    assert chip_watts([("cpu", 1000, "mJ"), ("cpu", 3000, "mJ"), ("gpu", 1000, "mJ")], 1.0) == \
        pytest.approx(5.0)  # an Ultra's two dies add up


@pytest.mark.parametrize("deltas, seconds", [
    ([("cpu", 1000, "mJ")], 1.0),  # no GPU channel
    ([("gpu", 1000, "mJ")], 1.0),  # no CPU channel
    ([("cpu", 1000, "mJ"), ("gpu", 1000, "kJ")], 1.0),  # a unit it doesn't know
    ([("cpu", 1000, "mJ"), ("gpu", -5, "mJ")], 1.0),  # a counter that went backwards
    ([("cpu", 0, "mJ"), ("gpu", 1500, "mJ")], 1.0),  # the Energy Model's frozen CPU counter on 27: GPU alone isn't Chip
    ([("cpu", 1000, "mJ"), ("gpu", 1000, "mJ")], 0.0),
    ([], 1.0),
])
def test_a_reading_that_cant_be_trusted_gives_none(deltas, seconds):
    assert chip_watts(deltas, seconds) is None


def test_deltas_go_by_channel_name():
    before = {"CPU Energy": ("cpu", 100, "nJ"), "GPU Energy": ("gpu", 50, "nJ")}
    after = {"GPU Energy": ("gpu", 80, "nJ"), "CPU Energy": ("cpu", 160, "nJ"), "ANE": ("ane", 7, "nJ")}
    assert sorted(counter_deltas(before, after)) == [("cpu", 60, "nJ"), ("gpu", 30, "nJ")]


class _Counters(ioreport.ChipEnergy):
    """ChipEnergy's reading over given counters, without IOReport. Each is (seconds, CPU nJ, GPU nJ),
    or (seconds, {channel name: (part, value, unit)}) for anything else."""

    def __init__(self, monkeypatch, *readings):
        self.source, self._sub, self._prev, self._watts, self._cpu_moved = ioreport.CLPC_SOURCE, True, None, None, False
        self.readings, self.resets = None, 0
        self.closed, self._readings, self._clock = False, list(readings), 0.0
        monkeypatch.setattr(ioreport.time, "monotonic", lambda: self._clock)

    def _counters(self):
        self._clock, *counters = self._readings.pop(0)
        if isinstance(counters[0], dict):
            return counters[0]
        cpu, gpu = counters
        return {"CPU Energy": ("cpu", cpu, "nJ"), "GPU Energy": ("gpu", gpu, "nJ")}

    def close(self):
        self.closed, self._sub = True, None


def test_the_first_reading_has_nothing_to_measure_from(monkeypatch):
    chip = _Counters(monkeypatch, (0.0, 0, 0), (1.0, 2e9, 1e9))
    assert chip.read() is None
    assert chip.read() == pytest.approx(3.0)


def test_a_reading_too_soon_for_the_counters_to_move_keeps_the_last(monkeypatch):
    # The menu wakes the sampler as it opens, which can be a few ms after a reading.
    chip = _Counters(monkeypatch, (0.0, 0, 0), (1.0, 2e9, 1e9), (1.002, 2e9, 1e9), (2.0, 4e9, 2e9))
    chip.read()
    assert chip.read() == pytest.approx(3.0)
    assert chip.read() == pytest.approx(3.0)  # nothing moved yet: the last reading stands
    assert chip.read() == pytest.approx(3.0)  # and the next spans the whole second since


def test_a_cpu_counter_that_never_moves_is_given_up_on(monkeypatch):
    # The Energy Model's on macOS 27: GPU Energy counts, CPU Energy doesn't.
    chip = _Counters(monkeypatch, (0.0, 5, 0), (1.0, 5, 1e9), (4.9, 5, 2e9), (5.0, 5, 3e9))
    assert chip.read() is None and chip.read() is None and chip.read() is None
    with pytest.raises(OSError, match="CPU counter isn't counting"):
        chip.read()
    assert chip.closed


def test_a_cpu_counter_that_stops_gives_no_reading_but_is_kept(monkeypatch):
    chip = _Counters(monkeypatch, (0.0, 0, 0), (1.0, 1e9, 1e9), (7.0, 1e9, 2e9), (8.0, 3e9, 3e9))
    chip.read()
    assert chip.read() == pytest.approx(2.0)
    assert chip.read() is None  # 6 s without moving: no stale number
    assert chip.read() == pytest.approx(4.0 / 7)  # it moved again: the energy since it last did
    assert not chip.closed


def test_a_counter_that_goes_backwards_starts_again_from_there(monkeypatch):
    # A reset: rather than waiting for it to climb past its old value, with Chip at "—" until then.
    chip = _Counters(monkeypatch, (0.0, 0, 0), (1.0, 5e9, 5e9), (2.0, 1e9, 6e9), (3.0, 3e9, 7e9))
    chip.read()
    assert chip.read() == pytest.approx(10.0)
    assert chip.read() == pytest.approx(10.0)  # the CPU counter reset: the last reading stands
    assert chip.read() == pytest.approx(3.0)  # and the next counts from the reset
    assert chip.resets == 1 and not chip.closed


def test_a_reading_that_cant_be_used_says_why(monkeypatch):
    # The M4's GPU report ID not matching on the Air: the subscription leaves the channel out.
    def cpu_only(value):
        return {"CPU Energy": ("cpu", value, "nJ"), "ANE": ("ane", 0, "nJ")}
    chip = _Counters(monkeypatch, (0.0, cpu_only(0)), (1.0, cpu_only(1.2e9)))
    chip.read()
    assert chip.read() is None
    assert describe_readings(chip.readings) == "CPU 1.20 W, no GPU counter, ANE 0.00 W"
    assert reading_problems(chip.readings) == ["no GPU counter"]


def test_samples_without_a_cpu_counter_say_so(monkeypatch):
    gpu_only = {"GPU Energy": ("gpu", 0, "nJ")}
    chip = _Counters(monkeypatch, (0.0, gpu_only), (1.0, gpu_only), (5.0, gpu_only))
    chip.read()
    assert chip.read() is None
    with pytest.raises(OSError, match="its samples have no CPU counter"):
        chip.read()
    assert describe_readings(chip.readings) == "no CPU counter, GPU 0.00 W, no ANE counter"


def test_each_part_says_what_it_read_or_what_is_wrong():
    readings = part_readings([("cpu", 1_200_000_000, "nJ"), ("gpu", 310_000, "uJ"), ("ane", 0, "nJ")], 1.0)
    assert describe_readings(readings) == "CPU 1.20 W, GPU 0.31 W, ANE 0.00 W" and reading_problems(readings) == []
    readings = part_readings([("cpu", 1000, "mJ"), ("gpu", 5, "xJ")], 1.0)
    assert describe_readings(readings) == 'CPU 1.00 W, GPU in "xJ", no ANE counter'
    assert reading_problems(readings) == ['GPU in "xJ"']  # a missing ANE counter doesn't stop Chip
    readings = part_readings([("cpu", 1000, "mJ"), ("gpu", -5, "mJ"), ("ane", 0, "mJ")], 1.0)
    assert reading_problems(readings) == ["GPU went backwards"]
    assert chip_watts([("cpu", 2000, "mJ"), ("gpu", 1000, "mJ")], 1.0) == pytest.approx(3.0)  # no ANE counter


def test_reads_this_mac():
    """Live read through the real IOReport (skipped where it can't be used)."""
    assert re.fullmatch(r"\d+(\.\d+)+", ioreport.macos_version())
    with objc.autorelease_pool():
        try:
            chip = ioreport.ChipEnergy()
        except OSError as e:
            pytest.skip(f"IOReport can't give Chip power here: {e}")
        try:
            assert chip.read() is None
            time.sleep(0.3)
            watts = chip.read()
        finally:
            chip.close()
    assert watts is not None and 0 < watts < 300, (chip.source, watts)
