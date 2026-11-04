"""Charging-session recording, storage, CSV export and the one-time legacy import."""

import csv
import json
import sqlite3

import pytest

from history import CURVE_FIELDS, LEGACY_IMPORT_KEY, ChargingRecorder, HistoryStore


def sample(t, level, charging=True, full=False, external=True, system_in=60.0, load=20.0):
    return {
        "t": t, "battery_level": level, "is_charging": charging, "fully_charged": full,
        "external_connected": external, "system_in": system_in if external else 0.0,
        "system_load": load, "battery_power": (system_in - load) if charging else -load,
        "adapter_power": system_in + 1.5 if external else 0.0, "efficiency_loss": 1.5,
        "screen_power": 5.0, "chip_power": 8.0, "temperature": 31.0,
        "adapter": {"name": "96W USB-C Power Adapter", "watts": 96} if external else None,
    }


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "history.db")


def charge(recorder, start_level, end_level, seconds, step=2, t0=1000.0):
    """Feed a charge that rises linearly, then an unplug sample."""
    n = seconds // step
    for i in range(n + 1):
        level = round(start_level + (end_level - start_level) * i / n)
        recorder.feed(sample(t0 + i * step, level))
    recorder.feed(sample(t0 + n * step + step, end_level, charging=False, external=False))


def test_charge_is_saved_with_summary_and_curve(store):
    charge(ChargingRecorder(store), 40, 55, seconds=600)
    [row] = store.list_sessions()
    assert (row["from_level"], row["to_level"]) == (40, 55)
    assert row["ended_at"] - row["started_at"] == 600
    assert row["adapter_name"] == "96W USB-C Power Adapter"
    assert row["avg_input_w"] == 61.5

    detail = store.get_session(row["id"])
    assert detail["curve_fields"] == list(CURVE_FIELDS)
    assert len(detail["curve"]) == 600 // 30 + 1  # one point per 30 s
    assert detail["curve"][-1][:2] == [1600, 55]
    assert detail["peak"]["battery_power"] == 40.0


def test_short_top_off_is_discarded(store):
    charge(ChargingRecorder(store), 99, 99, seconds=60)
    assert store.list_sessions() == []


def test_reaching_full_ends_the_session_at_100(store):
    rec = ChargingRecorder(store)
    for i in range(100):
        rec.feed(sample(1000 + i * 3, 90 + i // 10))
    rec.feed(sample(1300, 100, charging=False, full=True))
    [row] = store.list_sessions()
    assert row["to_level"] == 100


def test_disabling_recording_drops_the_session_in_progress(store):
    rec = ChargingRecorder(store)
    for i in range(100):
        rec.feed(sample(1000 + i * 3, 50))
    rec.enabled = False
    rec.feed(sample(1400, 60, charging=False))
    assert store.list_sessions() == [] and not rec.recording


def test_export_csv(store, tmp_path):
    charge(ChargingRecorder(store), 20, 30, seconds=300)
    [row] = store.list_sessions()
    out = tmp_path / "out.csv"
    assert store.export_csv(row["id"], out) > 0
    rows = list(csv.reader(out.open()))
    assert rows[0][:3] == ["time", "battery_level_pct", "adapter_input_w"]
    assert rows[1][1] == "20"


def test_delete_and_clear(store):
    rec = ChargingRecorder(store)
    charge(rec, 20, 30, seconds=300, t0=1000)
    charge(rec, 30, 40, seconds=300, t0=5000)
    first, second = store.list_sessions()
    assert store.delete_session(first["id"]) == 1
    assert [s["id"] for s in store.list_sessions()] == [second["id"]]
    assert store.clear() == 1 and store.list_sessions() == []


def make_legacy_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE charging_histories (id INTEGER PRIMARY KEY, from_level INTEGER, end_level INTEGER,"
               " charging_time INTEGER, timestamp INTEGER, detail BLOB, name TEXT, udid TEXT,"
               " is_remote INTEGER, adapter_name TEXT)")
    detail = {
        "avg": {"systemIn": 63.6, "systemLoad": 32.8, "batteryPower": 32.0, "adapterPower": 65.2,
                "efficiencyLoss": 1.56, "brightnessPower": 9.8, "heatpipePower": 14.0, "temperature": 31.0,
                "adapterWatts": 139.1},
        "peak": {"systemIn": 93.8, "adapterWatts": 140.0, "temperature": 33.7},
        "curve": [{"lastUpdate": 1790014754, "batteryLevel": 81, "systemIn": 3.4, "systemLoad": 30.5,
                   "batteryPower": 28.0, "temperature": 33.6, "timeRemain": {"secs": 3932100}},
                  {"lastUpdate": 1790017231, "batteryLevel": 99, "systemIn": 40.1, "systemLoad": 28.0,
                   "batteryPower": 12.1, "temperature": 31.0}],
        "raw": [],
    }
    rows = [
        (81, 99, 2477, 1790014754, json.dumps(detail).encode(), "Mac", "local", 0, "140W USB-C Power Adapter "),
        (10, 80, 3000, 1790000000, json.dumps(detail).encode(), "iPhone", "00008110-ABC", 1, "Charger"),
        (50, 60, 600, 1790001000, b"not json", "Mac", "local", 0, "x"),
    ]
    db.executemany("INSERT INTO charging_histories (from_level, end_level, charging_time, timestamp, detail,"
                   " name, udid, is_remote, adapter_name) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


def test_legacy_import_copies_local_sessions_once_and_reads_only(store, tmp_path):
    legacy = tmp_path / "db.sqlite"
    make_legacy_db(legacy)
    before = legacy.read_bytes()

    assert store.import_legacy(legacy) == 1  # iPhone row skipped, corrupt row skipped
    assert store.import_legacy(legacy) == 0  # never twice
    assert legacy.read_bytes() == before     # original untouched
    assert store.get_meta(LEGACY_IMPORT_KEY) == "imported 1"

    [row] = store.list_sessions()
    detail = store.get_session(row["id"])
    assert (detail["from_level"], detail["to_level"]) == (81, 99)
    assert detail["ended_at"] - detail["started_at"] == 2477
    assert detail["adapter_name"] == "140W USB-C Power Adapter"
    assert detail["adapter_watts"] == 140.0
    assert detail["avg"]["screen_power"] == 9.8 and "adapterWatts" not in detail["avg"]
    assert detail["source"] == "powerflow-0.2"
    assert detail["curve"][0] == [1790014754, 81, 3.4, 30.5, 28.0, 33.6]
    # Nothing identifying came across.
    assert "udid" not in json.dumps(detail) and "iPhone" not in json.dumps(detail)


def test_legacy_import_without_old_database(store, tmp_path):
    assert store.import_legacy(tmp_path / "missing.sqlite") == 0
    assert store.get_meta(LEGACY_IMPORT_KEY) == "no legacy database"


def test_charges_saved_with_a_third_party_charger_read_back_by_its_wattage(store):
    session = {"started_at": 1000.0, "ended_at": 1600.0, "from_level": 40, "to_level": 55,
               "adapter_name": "pd charger", "adapter_watts": 100, "avg": {}, "peak": {}, "curve": []}
    session_id = store.save_session(session)
    assert store.list_sessions()[0]["adapter_name"] == "100W USB-C Power Adapter"
    assert store.get_session(session_id)["adapter_name"] == "100W USB-C Power Adapter"

