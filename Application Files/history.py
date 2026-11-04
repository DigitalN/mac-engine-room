"""Charging-session history in a local SQLite database.

A session starts when the Mac begins charging and ends when it stops, is
unplugged, or reaches full. Only power/level numbers and the adapter's
marketing name are stored — no serial numbers, device names or identifiers.
"""

import csv
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from power import adapter_name  # sessions saved as "pd charger" read back by their wattage

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS charging_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER NOT NULL,
    from_level    INTEGER NOT NULL,
    to_level      INTEGER NOT NULL,
    adapter_name  TEXT    NOT NULL DEFAULT '',
    adapter_watts REAL,
    avg_json      TEXT    NOT NULL DEFAULT '{}',
    peak_json     TEXT    NOT NULL DEFAULT '{}',
    curve_json    TEXT    NOT NULL DEFAULT '[]',
    source        TEXT    NOT NULL DEFAULT 'pythonflow'
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON charging_sessions(started_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Stats averaged and peaked over every tick of a session.
STAT_FIELDS = ("system_in", "system_load", "battery_power", "adapter_power",
               "efficiency_loss", "screen_power", "chip_power", "temperature")
# One curve point per CURVE_INTERVAL_SECONDS, stored as compact arrays.
CURVE_FIELDS = ("t", "level", "system_in", "system_load", "battery_power", "temperature")
CURVE_INTERVAL_SECONDS = 30
MIN_SESSION_SECONDS = 120

LEGACY_IMPORT_KEY = "legacy_import_v0_2"
_LEGACY_STAT_KEYS = {
    "systemIn": "system_in", "systemLoad": "system_load", "batteryPower": "battery_power",
    "adapterPower": "adapter_power", "efficiencyLoss": "efficiency_loss",
    "brightnessPower": "screen_power", "heatpipePower": "chip_power", "temperature": "temperature",
}


class HistoryStore:
    """SQLite access. Each call opens its own connection, so any thread may use it."""

    def __init__(self, path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self._path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def save_session(self, session, source="pythonflow"):
        with self._lock, self._connect() as db:
            cur = db.execute(
                "INSERT INTO charging_sessions (started_at, ended_at, from_level, to_level, adapter_name,"
                " adapter_watts, avg_json, peak_json, curve_json, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    int(session["started_at"]), int(session["ended_at"]),
                    int(session["from_level"]), int(session["to_level"]),
                    session.get("adapter_name") or "", session.get("adapter_watts"),
                    json.dumps(session.get("avg") or {}), json.dumps(session.get("peak") or {}),
                    json.dumps(session.get("curve") or [], separators=(",", ":")), source,
                ),
            )
            return cur.lastrowid

    def list_sessions(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, started_at, ended_at, from_level, to_level, adapter_name, adapter_watts,"
                " avg_json, source FROM charging_sessions ORDER BY started_at DESC"
            ).fetchall()
        sessions = []
        for r in rows:
            avg = json.loads(r["avg_json"] or "{}")
            sessions.append({
                "id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                "from_level": r["from_level"], "to_level": r["to_level"],
                "adapter_name": adapter_name(r["adapter_name"], r["adapter_watts"]), "adapter_watts": r["adapter_watts"],
                "avg_input_w": avg.get("adapter_power") or avg.get("system_in"),
                "source": r["source"],
            })
        return sessions

    def get_session(self, session_id):
        with self._connect() as db:
            r = db.execute("SELECT * FROM charging_sessions WHERE id = ?", (int(session_id),)).fetchone()
        if r is None:
            return None
        return {
            "id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
            "from_level": r["from_level"], "to_level": r["to_level"],
            "adapter_name": adapter_name(r["adapter_name"], r["adapter_watts"]), "adapter_watts": r["adapter_watts"],
            "avg": json.loads(r["avg_json"] or "{}"), "peak": json.loads(r["peak_json"] or "{}"),
            "curve_fields": list(CURVE_FIELDS), "curve": json.loads(r["curve_json"] or "[]"),
            "source": r["source"],
        }

    def delete_session(self, session_id):
        with self._lock, self._connect() as db:
            return db.execute("DELETE FROM charging_sessions WHERE id = ?", (int(session_id),)).rowcount

    def clear(self):
        with self._lock, self._connect() as db:
            count = db.execute("DELETE FROM charging_sessions").rowcount
        with self._connect() as db:
            db.execute("VACUUM")
        return count

    def get_meta(self, key):
        with self._connect() as db:
            r = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key, value):
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def export_csv(self, session_id, path):
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"No session {session_id}")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time", "battery_level_pct", "adapter_input_w", "system_load_w",
                        "battery_power_w", "battery_temp_c"])
            for point in session["curve"]:
                row = dict(zip(CURVE_FIELDS, point))
                w.writerow([
                    datetime.fromtimestamp(row["t"]).isoformat(timespec="seconds"),
                    row.get("level"), row.get("system_in"), row.get("system_load"),
                    row.get("battery_power"), row.get("temperature"),
                ])
        return len(session["curve"])

    def import_legacy(self, legacy_db_path):
        """Copy this Mac's sessions from the Rust app's db.sqlite, once. Opens it read-only."""
        if self.get_meta(LEGACY_IMPORT_KEY):
            return 0
        legacy_db_path = Path(legacy_db_path)
        if not legacy_db_path.exists():
            self.set_meta(LEGACY_IMPORT_KEY, "no legacy database")
            return 0

        imported = 0
        src = sqlite3.connect(f"file:{legacy_db_path}?mode=ro", uri=True, timeout=10)
        try:
            rows = src.execute(
                "SELECT from_level, end_level, charging_time, timestamp, detail, adapter_name"
                " FROM charging_histories WHERE is_remote = 0 ORDER BY timestamp"
            ).fetchall()
        except sqlite3.Error:
            log.exception("Legacy database has an unexpected format; skipping import")
            rows = []
        finally:
            src.close()

        for from_level, end_level, charging_time, timestamp, detail, adapter_name in rows:
            try:
                session = legacy_row_to_session(from_level, end_level, charging_time, timestamp,
                                                detail, adapter_name)
            except (ValueError, KeyError, TypeError):
                log.warning("Skipping unreadable legacy session at %s", timestamp)
                continue
            self.save_session(session, source="powerflow-0.2")
            imported += 1

        self.set_meta(LEGACY_IMPORT_KEY, f"imported {imported}")
        log.info("Imported %d charging sessions from the previous Powerflow version", imported)
        return imported


def legacy_row_to_session(from_level, end_level, charging_time, timestamp, detail, adapter_name):
    if isinstance(detail, (bytes, bytearray)):
        detail = detail.decode("utf-8")
    d = json.loads(detail)

    def stats(src):
        return {new: round(src[old], 3) for old, new in _LEGACY_STAT_KEYS.items()
                if isinstance(src.get(old), (int, float))}

    curve = [
        [p["lastUpdate"], p.get("batteryLevel"), _r(p.get("systemIn")), _r(p.get("systemLoad")),
         _r(p.get("batteryPower")), _r(p.get("temperature"))]
        for p in d.get("curve", []) if isinstance(p.get("lastUpdate"), (int, float))
    ]
    peak = d.get("peak") or {}
    return {
        "started_at": int(timestamp), "ended_at": int(timestamp) + int(charging_time),
        "from_level": int(from_level), "to_level": int(end_level),
        "adapter_name": (adapter_name or "").strip(),
        "adapter_watts": peak.get("adapterWatts") or None,
        "avg": stats(d.get("avg") or {}), "peak": stats(peak), "curve": curve,
    }


def _r(value):
    return round(value, 2) if isinstance(value, (int, float)) else None


class ChargingRecorder:
    """Turns the stream of samples into charging sessions. Call feed() every tick."""

    def __init__(self, store, enabled=True):
        self._store = store
        self.enabled = enabled
        self._session = None

    @property
    def recording(self):
        return self._session is not None

    def feed(self, sample):
        if not self.enabled:
            self._session = None  # recording turned off: drop any session in progress
            return
        charging = (sample["is_charging"] and not sample["fully_charged"]
                    and sample["battery_level"] is not None)
        if self._session is not None:
            if charging:
                self._add(sample)
            else:
                self._finish(sample)
        elif charging:
            self._start(sample)

    def _start(self, s):
        self._session = {
            "started_at": s["t"], "from_level": s["battery_level"], "last_level": s["battery_level"],
            "adapter_name": None, "adapter_watts": None,
            "count": 0, "sums": {}, "counts": {}, "peak": {}, "curve": [], "last_point_at": 0.0,
        }
        self._add(s)

    def _add(self, s):
        sess = self._session
        sess["count"] += 1
        sess["last_level"] = s["battery_level"]
        sess["last_t"] = s["t"]
        adapter = s.get("adapter") or {}
        if adapter.get("name"):
            sess["adapter_name"] = adapter["name"]
        if adapter.get("watts"):
            sess["adapter_watts"] = adapter["watts"]
        for field in STAT_FIELDS:
            v = s.get(field)
            if isinstance(v, (int, float)):
                sess["sums"][field] = sess["sums"].get(field, 0.0) + v
                sess["counts"][field] = sess["counts"].get(field, 0) + 1
                sess["peak"][field] = max(sess["peak"].get(field, v), v)
        if s["t"] - sess["last_point_at"] >= CURVE_INTERVAL_SECONDS:
            sess["curve"].append([
                int(s["t"]), s["battery_level"], s.get("system_in"), s.get("system_load"),
                s.get("battery_power"), s.get("temperature"),
            ])
            sess["last_point_at"] = s["t"]

    def _finish(self, s):
        sess, self._session = self._session, None
        ended_at = sess.get("last_t", sess["started_at"])
        to_level = s["battery_level"] if s["battery_level"] is not None else sess["last_level"]
        if ended_at - sess["started_at"] < MIN_SESSION_SECONDS:
            return None
        # Close the curve at the last charging tick (not the stop sample, which may
        # arrive long after if the Mac slept) so the chart ends at the final level.
        last = sess["curve"][-1]
        if last[0] != int(ended_at) or last[1] != to_level:
            sess["curve"].append([int(ended_at), to_level, *last[2:]])
        session = {
            "started_at": sess["started_at"], "ended_at": ended_at,
            "from_level": sess["from_level"], "to_level": to_level,
            "adapter_name": sess["adapter_name"] or "Unknown adapter",
            "adapter_watts": sess["adapter_watts"],
            "avg": {k: round(sess["sums"][k] / sess["counts"][k], 3) for k in sess["sums"]},
            "peak": {k: round(v, 3) for k, v in sess["peak"].items()},
            "curve": sess["curve"],
        }
        try:
            session_id = self._store.save_session(session)
            log.info("Saved charging session %s: %s%% -> %s%% over %d min", session_id,
                     session["from_level"], to_level, (ended_at - sess["started_at"]) // 60)
            return session_id
        except Exception:
            log.exception("Could not save charging session")
            return None
