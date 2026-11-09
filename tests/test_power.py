"""normalize() against registry shapes seen on real Macs, plus SMC decoding."""

import logging
import plistlib
import struct
import sys
from types import SimpleNamespace

import pytest

import power
from power import decode_smc_value, normalize, parse_charge_limit

# AppleSmartBattery on macOS 27.0 (26A428), M1 Pro, charging on a 140 W adapter.
# AppleRaw*, DesignCapacity, AbsoluteCapacity and Temperature are gone from the top level.
MACOS_27 = {
    "AdapterDetails": {"Name": "140W USB-C Power Adapter", "Description": "pd charger", "Watts": 140,
                       "AdapterVoltage": 28000, "Current": 4990, "IsWireless": False},
    "Amperage": 8566, "AvgTimeToEmpty": 65535, "AvgTimeToFull": 77,
    "BatteryData": {"DesignCapacity": 8694, "FullChargeCapacity": 7798, "RemainingCapacity": 3042},
    "CurrentCapacity": 40, "CycleCount": 377, "ExternalConnected": True, "FullyCharged": False,
    "InstantAmperage": 8566, "IsCharging": True, "MaxCapacity": 100,
    "PowerTelemetryData": {"AdapterEfficiencyLoss": 3882, "BatteryPower": 105524,
                           "SystemLoad": 30883, "SystemPowerIn": 136407},
    "TimeRemaining": 77, "UpdateTime": 1790376450, "Voltage": 12319,
}
SMC_CHARGING = {"PDTR": 123.13, "PSTR": 28.63, "PPBR": 0.78, "PDBR": 10.43, "PHPC": 10.5,
                "TB0T": 33.4, "CHCC": 1.0, "B0TE": 65535.0, "B0TF": 71.0}

# Pre-27 Apple silicon: raw mAh values at the top level, no BatteryData.
LEGACY_APPLE_SILICON = {
    "AppleRawCurrentCapacity": 5932, "AppleRawMaxCapacity": 7756, "DesignCapacity": 8694,
    "CurrentCapacity": 81, "MaxCapacity": 100, "CycleCount": 376, "Temperature": 3059,
    "ExternalConnected": False, "IsCharging": False, "FullyCharged": False,
    "InstantAmperage": -1616, "TimeRemaining": 240, "Voltage": 12400,
}


def test_macos_27_reads_capacity_from_nested_battery_data():
    s = normalize(MACOS_27, SMC_CHARGING, now=1.0)
    assert s["battery_level"] == 40
    assert (s["design_capacity"], s["max_capacity"], s["current_capacity"]) == (8694, 7798, 3042)
    assert s["health_pct"] == pytest.approx(89.7, abs=0.05)
    assert s["cycle_count"] == 377
    assert s["temperature"] == 33.4  # from SMC; the registry no longer has it


def test_macos_27_power_flow_while_charging():
    s = normalize(MACOS_27, SMC_CHARGING, now=1.0)
    assert s["is_charging"] and s["external_connected"] and not s["fully_charged"]
    assert s["system_in"] == 123.13
    assert s["system_load"] == 28.63
    assert s["battery_power"] == pytest.approx(94.5, abs=0.01)
    assert s["efficiency_loss"] == 3.88
    assert s["adapter_power"] == pytest.approx(127.01, abs=0.01)
    assert s["time_remaining_min"] == 77
    assert s["adapter"] == {"name": "140W USB-C Power Adapter", "watts": 140, "voltage": 28.0,
                            "current": 4.99, "wireless": False}


def test_legacy_top_level_keys_still_work():
    smc = {"PSTR": 9.8, "PPBR": 9.9, "PDTR": 0.0, "TB0T": 30.6}
    s = normalize(LEGACY_APPLE_SILICON, smc, now=1.0)
    assert (s["design_capacity"], s["max_capacity"], s["current_capacity"]) == (8694, 7756, 5932)
    assert s["battery_level"] == 81
    assert not s["is_charging"] and not s["external_connected"]
    assert s["battery_power"] == -9.9  # discharging: PPBR, signed negative
    assert s["system_in"] == 0.0 and s["adapter_power"] == 0.0
    assert s["time_remaining_min"] == 240
    assert s["adapter"] is None


def test_registry_temperature_used_when_smc_missing():
    s = normalize(LEGACY_APPLE_SILICON, {}, now=1.0)
    assert s["temperature"] == 30.6  # centi-degrees -> °C
    assert s["has_smc"] is False
    assert s["battery_power"] == pytest.approx(-20.04, abs=0.01)  # amps × volts fallback


def test_intel_style_mah_capacity_gives_percentage():
    s = normalize({"CurrentCapacity": 3000, "MaxCapacity": 6000, "ExternalConnected": False}, {}, now=1.0)
    assert s["battery_level"] == 50


def test_fully_charged_on_adapter_is_not_charging_even_if_smc_says_so():
    battery = {**MACOS_27, "CurrentCapacity": 100, "FullyCharged": True, "IsCharging": False,
               "InstantAmperage": 0, "Amperage": 0, "TimeRemaining": 0}
    s = normalize(battery, {**SMC_CHARGING, "CHCC": 1.0}, now=1.0)
    assert s["fully_charged"] and not s["is_charging"]
    assert s["time_remaining_min"] is None


def test_amperage_sign_beats_stale_charging_flags():
    battery = {**MACOS_27, "IsCharging": True, "InstantAmperage": -250}
    assert normalize(battery, SMC_CHARGING, now=1.0)["is_charging"] is False


def test_unsigned_negative_current_is_treated_as_negative():
    battery = {**LEGACY_APPLE_SILICON, "ExternalConnected": True, "IsCharging": True,
               "InstantAmperage": 2**64 - 1500}
    s = normalize(battery, {}, now=1.0)
    assert s["is_charging"] is False
    assert s["amperage"] == -1.5


def test_time_remaining_sentinel_falls_back_then_gives_up():
    on_battery = {**LEGACY_APPLE_SILICON, "TimeRemaining": 65535, "AvgTimeToEmpty": 65535}
    assert normalize(on_battery, {"B0TE": 312.0}, now=1.0)["time_remaining_min"] == 312
    assert normalize(on_battery, {"B0TE": 65535.0}, now=1.0)["time_remaining_min"] is None


def test_plugged_in_not_charging_has_no_time_remaining():
    battery = {**MACOS_27, "IsCharging": False, "InstantAmperage": 0, "Amperage": 0}
    s = normalize(battery, {**SMC_CHARGING, "CHCC": 0.0}, now=1.0)
    assert s["external_connected"] and not s["is_charging"]
    assert s["time_remaining_min"] is None


def _charging_prefs(soclimit=80, reason="manualChargeLimit", terminated=False):
    """powerd's charging prefs as macOS 27.0.1 writes them, UUIDs zeroed."""
    uid = plistlib.UID
    archive = {
        "$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": uid(1)},
        "$objects": [
            "$null",
            {"$class": uid(7), "NS.objects": [uid(2)]},
            {"$class": uid(6), "drain": True, "isEndOfCharge": True, "noChargeToFull": False,
             "owner": 51041, "reason": uid(3), "soclimit": soclimit, "terminated": terminated,
             "token": uid(4)},
            reason,
            {"$class": uid(5), "NS.uuidbytes": bytes(16)},
            {"$classes": ["NSUUID", "NSObject"], "$classname": "NSUUID"},
            {"$classes": ["ChargeCtrlPolicy", "NSObject"], "$classname": "ChargeCtrlPolicy"},
            {"$classes": ["NSMutableArray", "NSArray", "NSObject"], "$classname": "NSMutableArray"},
        ],
    }
    policies = plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)
    return plistlib.dumps({"bootSessionUUID": "00000000-0000-0000-0000-000000000000",
                           "policies": policies}, fmt=plistlib.FMT_BINARY)


def test_charge_limit_is_read_from_powerd_prefs():
    assert parse_charge_limit(_charging_prefs()) == 80
    assert parse_charge_limit(_charging_prefs(soclimit=85)) == 85
    assert parse_charge_limit(_charging_prefs(soclimit=95)) == 95


def test_charge_limit_ignores_everything_but_a_manual_limit_below_100():
    assert parse_charge_limit(_charging_prefs(soclimit=100)) is None
    assert parse_charge_limit(_charging_prefs(reason="optimizedBatteryCharging")) is None
    assert parse_charge_limit(_charging_prefs(terminated=True)) is None
    assert parse_charge_limit(plistlib.dumps({"bootSessionUUID": "x"})) is None
    assert parse_charge_limit(b"not a plist") is None
    assert parse_charge_limit(b"") is None


def test_time_to_full_counts_to_the_charge_limit():
    # 40% to 80% of 7798 mAh at an averaged 8566 mA is 21.8 minutes, not macOS's 77 to 100%.
    s = normalize(MACOS_27, SMC_CHARGING, now=1.0, charge_limit=80)
    assert s["charge_limit"] == 80 and not s["at_charge_limit"]
    assert s["time_remaining_min"] == 22
    assert normalize(MACOS_27, SMC_CHARGING, now=1.0)["time_remaining_min"] == 77


def test_time_to_limit_is_about_a_minute_once_it_is_reached():
    battery = {**MACOS_27, "CurrentCapacity": 80}
    assert normalize(battery, SMC_CHARGING, now=1.0, charge_limit=80)["time_remaining_min"] == 1


def test_held_at_charge_limit():
    battery = {**MACOS_27, "IsCharging": False, "InstantAmperage": 0, "Amperage": 0,
               "CurrentCapacity": 78, "ChargerData": {"NotChargingReason": 0x1000000}}
    s = normalize(battery, {**SMC_CHARGING, "CHCC": 0.0}, now=1.0, charge_limit=80)
    assert s["at_charge_limit"] and not s["is_charging"]
    assert s["time_remaining_min"] is None
    # Not charging for another reason (here 0x80), or with no limit set: not held by it.
    other = {**battery, "ChargerData": {"NotChargingReason": 0x80}}
    assert not normalize(other, SMC_CHARGING, now=1.0, charge_limit=80)["at_charge_limit"]
    assert not normalize(battery, SMC_CHARGING, now=1.0)["at_charge_limit"]


def test_reaching_the_limit_stops_charging_before_the_current_winds_down():
    # Seen at 80%: the limit bit is set while IsCharging and the current still say charging.
    battery = {**MACOS_27, "CurrentCapacity": 80, "Amperage": 900, "InstantAmperage": 900,
               "ChargerData": {"NotChargingReason": 0x1000000}}
    s = normalize(battery, SMC_CHARGING, now=1.0, charge_limit=80)
    assert s["at_charge_limit"] and not s["is_charging"]
    assert s["time_remaining_min"] is None


def test_desktop_mac_without_battery():
    s = normalize(None, {"PSTR": 22.0, "PDTR": 23.5}, now=1.0)
    assert s["has_battery"] is False
    assert s["battery_level"] is None and s["battery_power"] is None
    assert s["system_load"] == 22.0


def test_empty_inputs_never_raise():
    s = normalize({}, {}, now=1.0)
    assert s["battery_level"] is None and s["system_load"] is None


def test_decode_smc_value_types():
    assert decode_smc_value("flt ", struct.pack("<f", 12.5), little_endian=True) == 12.5
    assert decode_smc_value("ui16", struct.pack("<H", 7821), little_endian=True) == 7821
    assert decode_smc_value("ui16", struct.pack(">H", 7821), little_endian=False) == 7821
    assert decode_smc_value("ui8 ", b"\x01", little_endian=True) == 1
    assert decode_smc_value("sp78", struct.pack(">h", int(30.5 * 256)), little_endian=True) == 30.5
    assert decode_smc_value("fpe2", struct.pack(">H", 4 * 100), little_endian=False) == 100.0
    assert decode_smc_value("????", b"\x00" * 4, little_endian=True) is None


def test_screen_power_comes_from_whichever_key_this_mac_has():
    s = normalize(None, {"PBLR": 7.204}, now=1.0)  # base M4
    assert s["screen_power"] == 7.2 and s["has_screen_power"]
    assert normalize(None, {"PDBR": 5.0, "PBLR": 7.2}, now=1.0)["screen_power"] == 5.0
    s = normalize(None, {"PBLR": 0.0}, now=1.0)  # a reading, but it doesn't show the sensor works
    assert s["screen_power"] == 0.0 and not s["has_screen_power"]
    s = normalize(None, {"PBLR": 0.01}, now=1.0)  # the M4 Air's: a hundredth of a watt isn't one either
    assert s["screen_power"] == 0.01 and not s["has_screen_power"]
    s = normalize(None, {"PHPC": 2.5}, now=1.0)  # a Mac with no screen sensor
    assert s["screen_power"] is None and not s["has_screen_power"]
    # A reading no screen could give doesn't count; the next key gets its turn.
    assert normalize(None, {"PDBR": 60.0, "PBLR": 7.2}, now=1.0)["screen_power"] == 7.2
    assert not normalize(None, {"PDBR": -1.0}, now=1.0)["has_screen_power"]


def test_a_reading_shows_its_sensor_works_from_a_tenth_of_a_watt():
    assert power.SENSOR_MIN_W == 0.1
    assert normalize(None, {"PBLR": 0.1}, now=1.0)["has_screen_power"]
    assert normalize(None, {"PHPC": 0.1}, now=1.0)["has_chip_power"]
    for watts in (0.0, 0.01, 0.09, 0.099):  # the last is judged as it is, not as the 0.1 it rounds to
        assert not normalize(None, {"PBLR": watts}, now=1.0)["has_screen_power"]
        assert not normalize(None, {"PHPC": watts}, now=1.0)["has_chip_power"]


def test_chip_power_comes_from_phpc():
    s = normalize(None, {"PHPC": 2.504}, now=1.0)
    assert s["chip_power"] == 2.5 and s["has_chip_power"]
    s = normalize(None, {"PHPC": 0.01}, now=1.0)  # the M4 Air's: read as it is, but not proof of a sensor
    assert s["chip_power"] == 0.01 and not s["has_chip_power"]
    s = normalize(None, {"PSTR": 9.0}, now=1.0)  # a Mac with no chip key
    assert s["chip_power"] is None and not s["has_chip_power"]
    assert not normalize(None, {"PHPC": -1.0}, now=1.0)["has_chip_power"]


def test_a_laptop_without_a_screen_key_asks_for_a_report_unless_known(monkeypatch):
    laptop = {"has_battery": True, "has_screen_power": False}
    assert power.screen_sensor_unknown(laptop, False, "MacBookPro18,3")
    # A key that reads next to nothing (a closed lid, a dark screen, or a dead key) doesn't ask.
    assert not power.screen_sensor_unknown(laptop, True, "MacBookPro18,3")
    assert not power.screen_sensor_unknown({**laptop, "has_battery": False}, False, "Mac16,10")  # a Mac mini
    assert not power.screen_sensor_unknown(None, False, "MacBookPro18,3")
    monkeypatch.setattr(power, "NO_SCREEN_SENSOR", frozenset({"MacBookPro18,3"}))
    assert not power.screen_sensor_unknown(laptop, False, "MacBookPro18,3")


def test_a_dead_chip_key_does_not_ask_for_a_report():
    # Only the screen keys decide: a laptop whose PHPC reads 0.01 W, like the M4 Air, is known.
    m4_air = {"has_battery": True, "has_screen_power": False, "has_chip_power": False}
    assert not power.screen_sensor_unknown(m4_air, True, "Mac16,12")
    assert not power.screen_sensor_unknown(m4_air, False, "Mac16,12")  # listed, so even without a screen key


def _every_key_but_screen():
    return {k: 1.0 for k in power.SMC_KEYS if k not in power.SCREEN_POWER_KEYS}


def test_missing_smc_keys_only_needs_one_screen_key():
    others = _every_key_but_screen()
    assert power.missing_smc_keys({**others, "PBLR": 7.2}) == []
    assert power.missing_smc_keys(others) == list(power.SCREEN_POWER_KEYS)
    del others["PHPC"]
    assert power.missing_smc_keys({**others, "PDBR": 5.0}) == ["PHPC"]


def _code(name):
    return struct.unpack(">I", name.encode())[0]


class _StubConnection(power.SMC):
    """The real SMC class with its IOKit call replaced. Keys map to (type, bytes), or None
    for one that refuses, as dozens do on a real Mac."""

    def __init__(self, keys):
        self._conn, self._key_info, self._little_endian = 0, {}, True
        self.keys, self.values_read = keys, []

    def _call(self, request):
        if request.data8 == power._SMC_CMD_KEY_BY_INDEX:
            return power._SMCKeyData(key=_code(list(self.keys)[request.data32]))
        name = struct.pack(">I", request.key).decode()
        if self.keys[name] is None:
            raise PermissionError("SMC refused")
        data_type, raw = self.keys[name]
        response = power._SMCKeyData()
        if request.data8 == power._SMC_CMD_READ_KEYINFO:
            response.key_info.data_size, response.key_info.data_type = len(raw), _code(data_type)
        else:
            self.values_read.append(name)
            response.bytes[:len(raw)] = list(raw)
        return response


def test_a_key_that_refuses_counts_as_missing_and_costs_no_other_reading():
    smc = _StubConnection({"PHPC": ("flt ", struct.pack("<f", 2.5)), "PBwo": None})
    assert smc.read_all(("PBwo", "PHPC")) == {"PHPC": 2.5}


def test_power_keys_only_reads_keys_starting_with_p():
    smc = _StubConnection({
        "#KEY": ("ui32", struct.pack(">I", 5)),  # big-endian, unlike the values
        "RSer": ("ch8*", b"IDENTIFYING"),
        "PBLR": ("flt ", struct.pack("<f", 7.25)),
        "PXXX": ("hex_", b"\x01"),
        "PNOP": None,
    })
    assert smc.power_keys() == ["PBLR=7.25", "PXXX(hex_)=?", "PNOP(refused)=?"]
    assert "RSer" not in smc.values_read


class _StubSMC:
    def __init__(self, *readings):
        self.readings, self.walks = list(readings), 0

    def read_all(self):
        return self.readings.pop(0)

    def power_keys(self):
        self.walks += 1
        return ["PHPC=2.50", "PZZZ=1.00"]


class _FakeChipEnergy:
    """ioreport.ChipEnergy with its readings given: watts, None, or an exception to raise."""
    source = "CLPC energy counters"

    def __init__(self, *readings):
        self.readings, self.closed = list(readings), False

    def read(self):
        reading = self.readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading

    def close(self):
        self.closed = True


def _no_ioreport():
    raise OSError("IOReport isn't opened in tests")


def _reader(monkeypatch, smc, model="MacBookPro18,1", chip_energy=_no_ioreport, forced=False):
    """A PowerReader on a Mac of this model (not the one running the tests). `chip_energy` makes
    the ChipEnergy where the reader opens IOReport: by default it can't, so no test opens the real
    one. `forced` sets the switch that reads Chip from IOReport where PHPC works."""
    monkeypatch.setattr(power, "SMC", lambda: smc)
    monkeypatch.setattr(power, "read_battery_registry", lambda: None)
    monkeypatch.setattr(power, "mac_model", lambda: model)
    monkeypatch.setitem(sys.modules, "ioreport", SimpleNamespace(ChipEnergy=chip_energy))
    if forced:
        monkeypatch.setenv(power.CHIP_IOREPORT_ENV, "1")
    else:
        monkeypatch.delenv(power.CHIP_IOREPORT_ENV, raising=False)
    return power.PowerReader()


def _missing_key_lines(caplog):
    return [r.getMessage() for r in caplog.records if "SMC keys missing" in r.getMessage()]


def test_missing_keys_are_logged_once_per_launch(monkeypatch, caplog):
    others = _every_key_but_screen()
    smc = _StubSMC(others, others)
    reader = _reader(monkeypatch, smc)
    with caplog.at_level(logging.INFO, logger="power"):
        reader.read()
        reader.read()
    [line] = _missing_key_lines(caplog)
    assert "PDBR, PBLR, PBwo" in line and "PZZZ=1.00" in line
    assert smc.walks == 1


def test_a_mac_with_every_key_logs_nothing_and_skips_the_walk(monkeypatch, caplog):
    smc = _StubSMC({**_every_key_but_screen(), "PBLR": 7.2})
    with caplog.at_level(logging.INFO, logger="power"):
        _reader(monkeypatch, smc).read()
    assert _missing_key_lines(caplog) == [] and smc.walks == 0


def test_the_check_waits_for_a_working_smc_read(monkeypatch, caplog):
    smc = _StubSMC({}, _every_key_but_screen())
    reader = _reader(monkeypatch, smc)
    with caplog.at_level(logging.INFO, logger="power"):
        reader.read()
        assert _missing_key_lines(caplog) == []
        reader.read()
    assert len(_missing_key_lines(caplog)) == 1


def test_screen_stays_offered_through_a_missed_reading(monkeypatch):
    reader = _reader(monkeypatch, _StubSMC({"PBLR": 7.2}, {}, {"PSTR": 9.0}))
    assert reader.read()["has_screen_power"]
    for _ in range(2):
        s = reader.read()
        assert s["screen_power"] is None and s["has_screen_power"]


# Screen (PBLR here, a base chip's key) and Chip (PHPC) follow the same rule.
SENSORS = [("screen", "PBLR"), ("chip", "PHPC")]


def test_a_screen_key_stuck_at_zero_shows_once_it_reads_real_watts(monkeypatch):
    # The M1 Air's PBLR: listed, but 0 at any brightness. Not shown, and not asked about.
    reader = _reader(monkeypatch, _StubSMC({"PBLR": 0.0}, {"PBLR": 0.0}, {"PBLR": 2.1}, {"PBLR": 0.0}))
    for _ in range(2):
        s = reader.read()
        assert not s["has_screen_power"] and s["screen_power"] is None and reader.has_screen_key
    assert reader.read()["screen_power"] == 2.1  # it works after all
    s = reader.read()  # and once it has, a dark screen reads 0 W
    assert s["has_screen_power"] and s["screen_power"] == 0.0


@pytest.mark.parametrize("name, key", SENSORS)
def test_a_key_stuck_at_a_hundredth_of_a_watt_never_shows(monkeypatch, name, key):
    # The M4 Air's PBLR and PHPC: listed, and 0.01 W even at 25 W of system load.
    reader = _reader(monkeypatch, _StubSMC(*[{key: 0.01, "PSTR": 25.0}] * 4))
    for _ in range(4):
        s = reader.read()
        assert not s[f"has_{name}_power"] and s[f"{name}_power"] is None


@pytest.mark.parametrize("name, key", SENSORS)
def test_a_key_shows_from_a_tenth_of_a_watt_and_then_stays(monkeypatch, name, key):
    reader = _reader(monkeypatch, _StubSMC({key: 0.09}, {key: 0.1}, {key: 0.0}, {}, {"PSTR": 9.0}))
    s = reader.read()
    assert not s[f"has_{name}_power"] and s[f"{name}_power"] is None
    s = reader.read()
    assert s[f"has_{name}_power"] and s[f"{name}_power"] == 0.1
    # It stays through a dark screen or an idle chip (0 W), and through a failed read or a
    # sample without the key (nothing).
    s = reader.read()
    assert s[f"has_{name}_power"] and s[f"{name}_power"] == 0.0
    for _ in range(2):
        s = reader.read()
        assert s[f"has_{name}_power"] and s[f"{name}_power"] is None


@pytest.mark.parametrize("model, name, key", [
    ("MacBookAir10,1", "screen", "PBLR"), ("Mac16,12", "screen", "PBLR"), ("Mac16,12", "chip", "PHPC")])
def test_a_model_known_to_lack_a_sensor_never_shows_it(monkeypatch, model, name, key):
    # Not from that key. Chip can still come from IOReport (see below); here it can't.
    reader = _reader(monkeypatch, _StubSMC(*[{key: 3.0}] * 2), model=model)  # even if a read looks real
    for _ in range(2):
        s = reader.read()
        assert not s[f"has_{name}_power"] and s[f"{name}_power"] is None


def test_the_m1_air_lacks_only_a_screen_sensor(monkeypatch):
    # Each list is its own. The M1 Air's sensor report: PBLR 0.00, PHPC 0.53.
    s = _reader(monkeypatch, _StubSMC({"PBLR": 0.0, "PHPC": 0.53}), model="MacBookAir10,1").read()
    assert not s["has_screen_power"] and s["has_chip_power"] and s["chip_power"] == 0.53


def test_only_a_mac_whose_phpc_cant_give_chip_wants_ioreport():
    assert not power.wants_ioreport("MacBookPro18,1", {"PHPC": 1.8})
    assert not power.wants_ioreport("Mac16,13", {"PHPC": 0.01})  # unlisted: hidden until its report lists it
    assert power.wants_ioreport("Mac16,12", {"PHPC": 0.01})  # the 13" M4 Air
    assert power.wants_ioreport("MacBookPro18,1", {"PSTR": 9.0})  # no PHPC key
    assert power.wants_ioreport("MacBookPro18,1", {"PHPC": 1.8}, forced=True)


def _chip_source_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(("Chip power from", "IOReport unavailable"))]


AIR_SMC = {"PHPC": 0.01, "PBLR": 0.01, "PSTR": 25.0}  # the 13" M4 Air's, under load


def test_a_mac_whose_phpc_works_never_opens_ioreport(monkeypatch, caplog):
    def opened():
        raise AssertionError("IOReport was opened")
    reader = _reader(monkeypatch, _StubSMC(*[{"PHPC": 1.8}] * 2), chip_energy=opened)
    with caplog.at_level(logging.INFO, logger="power"):
        for _ in range(2):
            s = reader.read()
    assert s["chip_power"] == 1.8 and s["has_chip_power"]
    assert _chip_source_lines(caplog) == ["Chip power from PHPC (reads 1.80 W)"]
    assert reader.ioreport_source is None and reader.ioreport_error is None


@pytest.mark.parametrize("model, smc, why", [
    ("Mac16,12", AIR_SMC, "PHPC reads 0.01 W"),
    ("MacBookPro18,1", {"PSTR": 9.0}, "PHPC missing"),
])
def test_chip_comes_from_ioreport_where_phpc_cant_give_it(monkeypatch, caplog, model, smc, why):
    reader = _reader(monkeypatch, _StubSMC(*[smc] * 5), model=model,
                     chip_energy=lambda: _FakeChipEnergy(None, 0.05, 2.504, 0.0, None))
    with caplog.at_level(logging.INFO, logger="power"):
        s = reader.read()  # the first reading has nothing to measure from
    assert s["chip_power"] is None and not s["has_chip_power"]
    s = reader.read()  # under SENSOR_MIN_W isn't proof yet, as with PHPC
    assert s["chip_power"] is None and not s["has_chip_power"]
    s = reader.read()
    assert s["chip_power"] == 2.5 and s["has_chip_power"]
    # Once shown it stays, through an idle chip and a reading with nothing.
    s = reader.read()
    assert s["chip_power"] == 0.0 and s["has_chip_power"]
    s = reader.read()
    assert s["chip_power"] is None and s["has_chip_power"]
    assert _chip_source_lines(caplog) == [f"Chip power from IOReport's CLPC energy counters ({why})"]
    assert reader.ioreport_source == "CLPC energy counters" and reader.ioreport_error is None


def test_the_switch_reads_chip_from_ioreport_where_phpc_works(monkeypatch, caplog):
    reader = _reader(monkeypatch, _StubSMC(*[{"PHPC": 12.0}] * 2), forced=True,
                     chip_energy=lambda: _FakeChipEnergy(None, 4.0))
    with caplog.at_level(logging.INFO, logger="power"):
        assert reader.read()["chip_power"] is None
        assert reader.read()["chip_power"] == 4.0  # IOReport's, not PHPC's
    assert _chip_source_lines(caplog) == [
        "Chip power from IOReport's CLPC energy counters (PYTHONFLOW_CHIP_IOREPORT=1; PHPC reads 12.00 W)"]


def test_without_ioreport_chip_stays_hidden_where_phpc_cant_give_it(monkeypatch, caplog):
    reader = _reader(monkeypatch, _StubSMC(*[AIR_SMC] * 2), model="Mac16,12")
    with caplog.at_level(logging.INFO, logger="power"):
        for _ in range(2):
            s = reader.read()
            assert not s["has_chip_power"] and s["chip_power"] is None
    assert _chip_source_lines(caplog) == ["IOReport unavailable: IOReport isn't opened in tests (PHPC reads 0.01 W)"]
    assert reader.ioreport_error == "IOReport isn't opened in tests" and reader.ioreport_source is None


def test_anything_ioreport_does_wrong_only_costs_chip(monkeypatch):
    def broken():
        raise ValueError("bad channel")
    s = _reader(monkeypatch, _StubSMC(AIR_SMC), model="Mac16,12", chip_energy=broken).read()
    assert s["system_load"] == 25.0 and not s["has_chip_power"] and s["chip_power"] is None


@pytest.mark.parametrize("error", [OSError("the CPU counter isn't counting (Energy Model)"), RuntimeError("boom")])
def test_ioreport_that_stops_working_hides_chip_again(monkeypatch, caplog, error):
    chip = _FakeChipEnergy(None, 3.0, error)
    reader = _reader(monkeypatch, _StubSMC(*[AIR_SMC] * 4), model="Mac16,12", chip_energy=lambda: chip)
    reader.read()
    assert reader.read()["has_chip_power"]
    with caplog.at_level(logging.INFO, logger="power"):
        for _ in range(2):  # and it isn't read again
            s = reader.read()
            assert not s["has_chip_power"] and s["chip_power"] is None
    assert chip.closed and reader.ioreport_source is None and reader.ioreport_error == str(error)
    assert _chip_source_lines(caplog) == [f"IOReport unavailable: {error} (PHPC reads 0.01 W)"]


@pytest.mark.parametrize("name", ["screen", "chip"])
def test_a_mac_with_no_sensor_never_offers_it(monkeypatch, name):
    reader = _reader(monkeypatch, _StubSMC({"PSTR": 9.0}, {"PSTR": 9.0}))
    assert not reader.read()[f"has_{name}_power"]
    assert not reader.read()[f"has_{name}_power"]


def test_reads_this_mac():
    """Live read through the real IOKit/SMC path (skipped where there's nothing to read)."""
    sample = power.PowerReader().read()
    if not sample["has_battery"] and not sample["has_smc"]:
        pytest.skip("no battery registry or SMC on this machine")
    assert set(sample) >= {"system_load", "battery_level", "is_charging", "adapter"}
    if sample["has_battery"]:
        assert 0 <= sample["battery_level"] <= 100


def test_every_charger_is_named_by_its_wattage():
    assert power.adapter_name("140W USB-C Power Adapter", 140) == "140W USB-C Power Adapter"  # Apple's own
    assert power.adapter_name("pd charger", 100) == "100W USB-C Power Adapter"  # any other brand
    assert power.adapter_name(None, 65.0) == "65W USB-C Power Adapter"
    assert power.adapter_name("35W Dual USB-C Port Power Adapter", 35) == "35W Dual USB-C Port Power Adapter"
    assert power.adapter_name("pd charger", None) == "pd charger" and power.adapter_name("", 0) is None
    third_party = {**MACOS_27, "AdapterDetails": {"Description": "pd charger", "Watts": 100, "IsWireless": False}}
    assert normalize(third_party, {}, now=1.0)["adapter"]["name"] == "100W USB-C Power Adapter"
    assert normalize(MACOS_27, {}, now=1.0)["adapter"]["name"] == "140W USB-C Power Adapter"

