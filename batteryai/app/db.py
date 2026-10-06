"""SQLite storage for sensor readings and AI analyses."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    minute_of_day INTEGER NOT NULL,
    weekday TEXT NOT NULL,
    is_weekend INTEGER NOT NULL,
    battery_soc REAL,
    today_forecast REAL,
    tomorrow_forecast REAL,
    today_load REAL,
    today_consumption REAL,
    outages_state TEXT,
    outages_attrs TEXT,
    deye_programs TEXT,
    target_soc REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);
CREATE INDEX IF NOT EXISTS idx_readings_date ON readings (local_date);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    finished_ts INTEGER,
    trigger TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,
    summary TEXT,
    result_json TEXT,
    input_json TEXT,
    error TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_analyses_ts ON analyses (ts);

-- Monthly bill: grid energy per day and tariff with the cost at the price in effect when it
-- was recorded, so later tariff changes leave past months as they were.
CREATE TABLE IF NOT EXISTS bill_days (
    local_date TEXT NOT NULL,
    tariff TEXT NOT NULL,
    kwh REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (local_date, tariff)
);

-- Energy of each appliance per day and tariff, recorded live from its power sensor from the
-- moment it is configured (never backfilled). Its cost uses the month's price (bill_prices).
CREATE TABLE IF NOT EXISTS bill_appliances (
    local_date TEXT NOT NULL,
    appliance TEXT NOT NULL,
    tariff TEXT NOT NULL,
    kwh REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (local_date, appliance, tariff)
);

-- The price per kWh of each tariff in each month: taken from the settings when the month's
-- first energy of that tariff is recorded, editable in the Monthly bill.
CREATE TABLE IF NOT EXISTS bill_prices (
    month TEXT NOT NULL,
    tariff TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (month, tariff)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after 0.2.0 (today_load is kept only for old rows; see _migrate).
NEW_READING_COLUMNS = {
    "load_power": "REAL",
    "heat_pump_power": "REAL",
    "boiler_power": "REAL",
    "ev_power": "REAL",
    "outdoor_temp": "REAL",
    "pv_today": "REAL",
    "grid_import_today": "REAL",
    "pv_power": "REAL",
    "control_mode": "TEXT",
    "source": "TEXT DEFAULT 'live'",
    "resolution_s": "INTEGER",
    "appliances": "TEXT",  # JSON {appliance id: W}; replaces the three fixed columns above
}
NEW_ANALYSIS_COLUMNS = {"actions_json": "TEXT"}

READING_FIELDS = (
    "battery_soc",
    "today_forecast",
    "tomorrow_forecast",
    "today_consumption",
    "load_power",
    "appliances",
    "outdoor_temp",
    "pv_today",
    "grid_import_today",
    "pv_power",
    "control_mode",
    "resolution_s",
)
# Older readings are compressed to one row per hour (see compress_before).
HOURLY = 3600
AVERAGED = ("battery_soc", "load_power", "outdoor_temp", "pv_power")

# Daily energy counters may still show yesterday's total for a few minutes after midnight.
DAILY_RESET_GRACE_MINUTES = 15
# Longest gap between two readings that still counts as continuous when integrating power.
MAX_INTEGRATION_GAP = 15 * 60
# An appliance counts as running during an hour when its average power is above this.
ACTIVE_POWER_W = 200


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._last_attrs: str | None = None
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        def columns(table: str) -> set[str]:
            return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}

        existing = columns("readings")
        for name, kind in NEW_READING_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE readings ADD COLUMN {name} {kind}")
                if name == "load_power":
                    # Up to 0.2.0 the "today load" setting held a load power sensor (W).
                    self._conn.execute("UPDATE readings SET load_power = today_load")
                if name == "appliances" and {"heat_pump_power", "boiler_power", "ev_power"} <= existing:
                    # Up to 0.4.0 three fixed appliances had their own columns.
                    rows = self._conn.execute(
                        "SELECT id, heat_pump_power, boiler_power, ev_power FROM readings "
                        "WHERE heat_pump_power IS NOT NULL OR boiler_power IS NOT NULL OR ev_power IS NOT NULL"
                    ).fetchall()
                    self._conn.executemany(
                        "UPDATE readings SET appliances = ? WHERE id = ?",
                        [
                            (json.dumps({k: v for k, v in (("heat_pump", r[1]), ("boiler", r[2]), ("ev", r[3])) if v is not None}), r[0])
                            for r in rows
                        ],
                    )
        # 0.4.10 stored a single price without a tariff name.
        self._conn.execute("UPDATE OR IGNORE bill_days SET tariff = 'Single price' WHERE tariff = ''")
        # Months recorded by 0.4.10/0.4.11 get the price their costs were recorded at.
        self._conn.execute(
            "INSERT OR IGNORE INTO bill_prices (month, tariff, price) "
            "SELECT substr(local_date, 1, 7), tariff, SUM(cost) / SUM(kwh) FROM bill_days "
            "GROUP BY 1, 2 HAVING SUM(kwh) > 0"
        )
        existing = columns("analyses")
        for name, kind in NEW_ANALYSIS_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE analyses ADD COLUMN {name} {kind}")

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # Readings -----------------------------------------------------------

    @staticmethod
    def _reading_values(snap: dict[str, Any], tz: tzinfo, source: str) -> tuple:
        local = datetime.fromtimestamp(snap["ts"], tz)
        active = next(
            (p for p in snap.get("deye_programs") or [] if p["slot"] == snap.get("active_program_slot")), None
        )
        return (
            snap["ts"],
            local.date().isoformat(),
            local.hour * 60 + local.minute,
            snap["weekday"],
            int(snap["is_weekend"]),
            *(_encode(field, snap.get(field)) for field in READING_FIELDS),
            snap.get("outages_state"),
            json.dumps(snap.get("outages_attrs") or {}, default=str, ensure_ascii=False),
            json.dumps(snap.get("deye_programs") or []),
            active["soc"] if active else None,
            source,
        )

    _INSERT = (
        "INSERT INTO readings (ts, local_date, minute_of_day, weekday, is_weekend, "
        + ", ".join(READING_FIELDS)
        + ", outages_state, outages_attrs, deye_programs, target_soc, source) VALUES ("
        + ", ".join("?" * (5 + len(READING_FIELDS) + 5))
        + ")"
    )

    def add_reading(self, snap: dict[str, Any], tz: tzinfo) -> None:
        values = list(self._reading_values(snap, tz, "live"))
        # Outage attributes can be large and rarely change: store them only when they do.
        attrs_index = 5 + len(READING_FIELDS) + 1
        if values[attrs_index] == self._last_attrs:
            values[attrs_index] = None
        else:
            self._last_attrs = values[attrs_index]
        self._execute(self._INSERT, tuple(values))

    def replace_range(self, start_ts: int, end_ts: int, snaps: list[dict[str, Any]], tz: tzinfo) -> None:
        """Replaces all readings in [start_ts, end_ts) with imported history."""
        with self._lock:
            self._conn.execute("DELETE FROM readings WHERE ts >= ? AND ts < ?", (start_ts, end_ts))
            self._conn.executemany(self._INSERT, [self._reading_values(s, tz, "history") for s in snaps])
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        path = self._conn.execute("PRAGMA database_list").fetchone()["file"]
        files = {suffix: os.path.getsize(path + suffix) for suffix in ("", "-wal", "-shm") if os.path.exists(path + suffix)}
        tables = {}
        for name in ("readings", "analyses", "bill_days", "bill_appliances"):
            tables[name] = self._query(f"SELECT COUNT(*) AS n FROM {name}")[0]["n"]
        span = self._query("SELECT MIN(ts) AS first, MAX(ts) AS last FROM readings")[0]
        sources = {row["source"] or "live": row["n"] for row in self._query("SELECT source, COUNT(*) AS n FROM readings GROUP BY source")}
        detail_since = self._query("SELECT MIN(ts) AS ts FROM readings WHERE resolution_s IS NULL OR resolution_s < 3600")[0]["ts"]
        page = self._query("PRAGMA page_count")[0]["page_count"] * self._query("PRAGMA page_size")[0]["page_size"]
        free = self._query("PRAGMA freelist_count")[0]["freelist_count"] * self._query("PRAGMA page_size")[0]["page_size"]
        return {
            "path": path,
            "file_bytes": sum(files.values()),
            "files": files,
            "allocated_bytes": page,
            "free_bytes": free,
            "rows": tables,
            "reading_sources": sources,
            "first_reading": span["first"],
            "detail_since": detail_since,
            "last_reading": span["last"],
        }

    def compress_before(self, cutoff_ts: int) -> dict[str, int]:
        """Replaces detailed readings older than cutoff_ts by one row per hour.

        Power, SOC and temperature become the hour's average (so energy totals stay right);
        counters, forecasts, programs and states keep the hour's last value. Outage
        attributes are dropped from compressed rows.
        """
        removed = added = 0
        while True:
            with self._lock:
                rows = [
                    dict(r)
                    for r in self._conn.execute(
                        "SELECT * FROM readings WHERE ts < ? AND (resolution_s IS NULL OR resolution_s < ?) "
                        "ORDER BY ts LIMIT 20000",
                        (cutoff_ts, HOURLY),
                    ).fetchall()
                ]
            if not rows:
                break
            # Don't split the last hour of a batch across two batches.
            last_bucket = rows[-1]["ts"] // HOURLY
            if len(rows) == 20000:
                rows = [r for r in rows if r["ts"] // HOURLY < last_bucket] or rows
            buckets: dict[int, list[dict[str, Any]]] = {}
            for row in rows:
                buckets.setdefault(row["ts"] // HOURLY, []).append(row)
            new_rows = []
            for bucket, items in buckets.items():
                last = items[-1]
                merged = dict(last)
                merged["ts"] = bucket * HOURLY
                merged["minute_of_day"] = items[0]["minute_of_day"] - items[0]["minute_of_day"] % 60
                merged["local_date"] = items[0]["local_date"]
                for name in AVERAGED:
                    values = [r[name] for r in items if r[name] is not None]
                    merged[name] = sum(values) / len(values) if values else None
                per_appliance: dict[str, list[float]] = {}
                for r in items:
                    for app_id, watts in (json.loads(r["appliances"]) if r.get("appliances") else {}).items():
                        if watts is not None:
                            per_appliance.setdefault(app_id, []).append(watts)
                merged["appliances"] = (
                    json.dumps({k: sum(v) / len(v) for k, v in per_appliance.items()}) if per_appliance else None
                )
                merged["outages_attrs"] = None
                merged["resolution_s"] = HOURLY
                merged["source"] = "hourly"
                new_rows.append(merged)
            columns = [c for c in new_rows[0] if c != "id"]
            with self._lock:
                self._conn.executemany(
                    "DELETE FROM readings WHERE id = ?", [(r["id"],) for items in buckets.values() for r in items]
                )
                self._conn.executemany(
                    f"INSERT INTO readings ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    [tuple(r[c] for c in columns) for r in new_rows],
                )
                self._conn.commit()
            removed += sum(len(items) for items in buckets.values())
            added += len(new_rows)
        return {"removed": removed, "added": added}

    def backup_to(self, path: str) -> None:
        """A consistent copy of the whole database (SQLite online backup)."""
        with self._lock:
            target = sqlite3.connect(path)
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")

    def relocalize(self, tz: tzinfo, weekend_days: list[str]) -> int:
        """Recomputes local_date / minute_of_day / weekday / is_weekend from ts in tz."""
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, local_date, minute_of_day, weekday, is_weekend FROM readings"
            ).fetchall()
            updates = []
            for row in rows:
                local = datetime.fromtimestamp(row["ts"], tz)
                weekday = weekdays[local.weekday()]
                values = (local.date().isoformat(), local.hour * 60 + local.minute, weekday, int(weekday in weekend_days))
                if values != (row["local_date"], row["minute_of_day"], row["weekday"], row["is_weekend"]):
                    updates.append((*values, row["id"]))
            self._conn.executemany(
                "UPDATE readings SET local_date = ?, minute_of_day = ?, weekday = ?, is_weekend = ? WHERE id = ?",
                updates,
            )
            self._conn.commit()
        return len(updates)

    def recompute_targets(self, target_for: Callable[[list[dict[str, Any]], int], float | None]) -> int:
        """Re-derives target_soc of every reading from its stored programs (after the
        program-time meaning changes, or for rows recorded before programs were configured)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, minute_of_day, deye_programs FROM readings").fetchall()
            updates = []
            for row in rows:
                programs = json.loads(row["deye_programs"] or "[]")
                updates.append((target_for(programs, row["minute_of_day"]) if programs else None, row["id"]))
            self._conn.executemany("UPDATE readings SET target_soc = ? WHERE id = ?", updates)
            self._conn.commit()
        return len(updates)

    def readings_since(self, since_ts: int) -> list[dict[str, Any]]:
        """Readings from since_ts on; "appliances" is a dict {appliance id: W}."""
        return _decode_appliances(self._query(
            "SELECT ts, local_date, minute_of_day, weekday, is_weekend, "
            + ", ".join(READING_FIELDS)
            + ", outages_state, target_soc FROM readings WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        ))

    def latest_reading(self) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM readings ORDER BY ts DESC LIMIT 1")
        if not rows:
            return None
        row = rows[0]
        row["deye_programs"] = json.loads(row["deye_programs"] or "[]")
        _decode_appliances([row])
        if row["outages_attrs"] is None:  # unchanged since an earlier reading
            earlier = self._query(
                "SELECT outages_attrs FROM readings WHERE outages_attrs IS NOT NULL ORDER BY ts DESC LIMIT 1"
            )
            row["outages_attrs"] = earlier[0]["outages_attrs"] if earlier else None
        row["outages_attrs"] = json.loads(row["outages_attrs"] or "{}")
        return row

    def reading_count(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM readings")[0]["n"]

    def earliest_ts(self) -> int | None:
        return self._query("SELECT MIN(ts) AS ts FROM readings")[0]["ts"]

    def daily_summary(self, days: int, today: date) -> list[dict[str, Any]]:
        first = (today - timedelta(days=days - 1)).isoformat()
        rows = _decode_appliances(self._query(
            "SELECT ts, local_date, minute_of_day, weekday, is_weekend, "
            + ", ".join(READING_FIELDS)
            + ", outages_state FROM readings WHERE local_date >= ? ORDER BY ts",
            (first,),
        ))
        days_out: dict[str, dict[str, Any]] = {}
        hourly_power: dict[tuple[str, str, int], list[float]] = {}
        for index, row in enumerate(rows):
            day = days_out.get(row["local_date"])
            if day is None:
                day = days_out[row["local_date"]] = {
                    "date": row["local_date"],
                    "weekday": row["weekday"],
                    "is_weekend": bool(row["is_weekend"]),
                    "consumption_kwh": None,
                    "pv_kwh": None,
                    "grid_import_kwh": None,
                    "solar_forecast_kwh": row["today_forecast"],  # forecast at the start of the day
                    "min_soc": None,
                    "max_soc": None,
                    "temp_min": None,
                    "temp_max": None,
                    "_temps": [],
                    "peak_load_w": None,
                    "load_kwh": 0.0,
                    "appliances": {},  # id -> {"kwh", "active_hours"}
                    "outage_states": [],
                    "samples": 0,
                }
            day["samples"] += 1
            if day["solar_forecast_kwh"] is None:
                day["solar_forecast_kwh"] = row["today_forecast"]
            if row["minute_of_day"] >= DAILY_RESET_GRACE_MINUTES:
                day["consumption_kwh"] = _max(day["consumption_kwh"], row["today_consumption"])
                day["pv_kwh"] = _max(day["pv_kwh"], row["pv_today"])
                day["grid_import_kwh"] = _max(day["grid_import_kwh"], row["grid_import_today"])
            soc = row["battery_soc"]
            if soc is not None:
                day["min_soc"] = soc if day["min_soc"] is None else min(day["min_soc"], soc)
                day["max_soc"] = _max(day["max_soc"], soc)
            temp = row["outdoor_temp"]
            if temp is not None:
                day["temp_min"] = temp if day["temp_min"] is None else min(day["temp_min"], temp)
                day["temp_max"] = _max(day["temp_max"], temp)
                day["_temps"].append(temp)
            day["peak_load_w"] = _max(day["peak_load_w"], row["load_power"])

            # Integrate power (W) over time until the next reading.
            if index + 1 < len(rows):
                gap = min(rows[index + 1]["ts"] - row["ts"], max(MAX_INTEGRATION_GAP, row["resolution_s"] or 0))
                hour = row["minute_of_day"] // 60
                if row["load_power"] is not None:
                    day["load_kwh"] += row["load_power"] * gap / 3_600_000
                for app_id, watts in row["appliances"].items():
                    if watts is None:
                        continue
                    entry = day["appliances"].setdefault(app_id, {"kwh": 0.0, "active_hours": []})
                    entry["kwh"] += watts * gap / 3_600_000
                    hourly_power.setdefault((row["local_date"], app_id, hour), []).append(watts)

            state = row["outages_state"]
            if state and state not in day["outage_states"] and len(day["outage_states"]) < 10:
                day["outage_states"].append(state)

        for day in days_out.values():
            temps = day.pop("_temps")
            day["temp_avg"] = round(sum(temps) / len(temps), 1) if temps else None
            day["load_kwh"] = round(day["load_kwh"], 2)
            for app_id, entry in day["appliances"].items():
                entry["kwh"] = round(entry["kwh"], 2)
                entry["active_hours"] = sorted(
                    hour
                    for (date_key, appliance, hour), values in hourly_power.items()
                    if date_key == day["date"] and appliance == app_id and sum(values) / len(values) >= ACTIVE_POWER_W
                )
        return list(days_out.values())

    def hourly_profile(self, since_ts: int) -> list[dict[str, Any]]:
        """Average power per hour of day (load, PV, each appliance), split into weekdays and weekends."""
        rows = _decode_appliances(self._query(
            "SELECT is_weekend, minute_of_day, load_power, pv_power, outdoor_temp, appliances "
            "FROM readings WHERE ts >= ?",
            (since_ts,),
        ))
        sums: dict[tuple[int, int], dict[str, list[float]]] = {}
        for row in rows:
            bucket = sums.setdefault((row["is_weekend"], row["minute_of_day"] // 60), {})
            for key in ("load_power", "pv_power", "outdoor_temp"):
                if row[key] is not None:
                    bucket.setdefault(key, []).append(row[key])
            for app_id, watts in row["appliances"].items():
                if watts is not None:
                    bucket.setdefault(f"appliance:{app_id}", []).append(watts)
        out = []
        for (is_weekend, hour), values in sorted(sums.items()):
            entry: dict[str, Any] = {"is_weekend": is_weekend, "hour": hour, "appliances_w": {}}
            for key, items in values.items():
                avg = round(sum(items) / len(items), 1)
                if key.startswith("appliance:"):
                    entry["appliances_w"][key.split(":", 1)[1]] = avg
                else:
                    entry[key] = avg
            out.append(entry)
        return out

    # Monthly bill -------------------------------------------------------

    def meta_get(self, key: str) -> Any:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows and rows[0]["value"] else None

    def meta_set(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def add_bill(self, entries: dict[tuple[str, str], tuple[float, float]], meter_key: str, meter: Any) -> None:
        """Adds kWh per (date, tariff) and stores the meter state, in one transaction. The cost
        uses the month's price of the tariff; a month's first energy of a tariff sets that
        price from the settings price in entries (cost / kWh)."""
        with self._lock:
            for (day, tariff), (kwh, cost) in entries.items():
                month = day[:7]
                if kwh > 0:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO bill_prices (month, tariff, price) VALUES (?, ?, ?)",
                        (month, tariff, cost / kwh),
                    )
                row = self._conn.execute(
                    "SELECT price FROM bill_prices WHERE month = ? AND tariff = ?", (month, tariff)
                ).fetchone()
                self._conn.execute(
                    "INSERT INTO bill_days (local_date, tariff, kwh, cost) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(local_date, tariff) DO UPDATE SET kwh = kwh + excluded.kwh, cost = cost + excluded.cost",
                    (day, tariff, kwh, kwh * row["price"] if row else cost),
                )
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (meter_key, json.dumps(meter)),
            )
            self._conn.commit()

    def replace_bill_month(
        self, month: str, entries: dict[tuple[str, str], tuple[float, float]], prices: dict[str, float]
    ) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM bill_days WHERE substr(local_date, 1, 7) = ?", (month,))
            self._conn.execute("DELETE FROM bill_prices WHERE month = ?", (month,))
            self._conn.executemany(
                "INSERT INTO bill_days (local_date, tariff, kwh, cost) VALUES (?, ?, ?, ?)",
                [(day, tariff, kwh, cost) for (day, tariff), (kwh, cost) in entries.items()],
            )
            self._conn.executemany(
                "INSERT INTO bill_prices (month, tariff, price) VALUES (?, ?, ?)",
                [(month, tariff, price) for tariff, price in prices.items()],
            )
            self._conn.commit()

    def set_bill_price(self, month: str, tariff: str, price: float) -> None:
        """A month's price of one tariff, edited in the Monthly bill; its costs follow."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO bill_prices (month, tariff, price) VALUES (?, ?, ?) "
                "ON CONFLICT(month, tariff) DO UPDATE SET price = excluded.price",
                (month, tariff, price),
            )
            self._conn.execute(
                "UPDATE bill_days SET cost = kwh * ? WHERE substr(local_date, 1, 7) = ? AND tariff = ?",
                (price, month, tariff),
            )
            self._conn.commit()

    def add_bill_appliances(
        self, entries: dict[tuple[str, str, str], float], prices: dict[str, float], meter_key: str, state: Any
    ) -> None:
        """Adds appliance kWh per (date, appliance, tariff); a month without a price for the
        tariff yet takes it from prices (the settings). Stores the sampling state."""
        with self._lock:
            for (day, appliance, tariff), kwh in entries.items():
                if tariff in prices:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO bill_prices (month, tariff, price) VALUES (?, ?, ?)",
                        (day[:7], tariff, prices[tariff]),
                    )
                self._conn.execute(
                    "INSERT INTO bill_appliances (local_date, appliance, tariff, kwh) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(local_date, appliance, tariff) DO UPDATE SET kwh = kwh + excluded.kwh",
                    (day, appliance, tariff, kwh),
                )
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (meter_key, json.dumps(state)),
            )
            self._conn.commit()

    def bill_appliance_rows(self, first_date: str, last_date: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT local_date, appliance, tariff, kwh FROM bill_appliances WHERE local_date >= ? AND local_date <= ? "
            "ORDER BY local_date, appliance, tariff",
            (first_date, last_date),
        )

    def merge_appliance_tariffs(self, month: str, tariff: str) -> None:
        """A month switched to a single price: its appliance energy goes under that tariff."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT local_date, appliance, SUM(kwh) AS kwh FROM bill_appliances "
                "WHERE substr(local_date, 1, 7) = ? GROUP BY local_date, appliance", (month,)
            ).fetchall()
            self._conn.execute("DELETE FROM bill_appliances WHERE substr(local_date, 1, 7) = ?", (month,))
            self._conn.executemany(
                "INSERT INTO bill_appliances (local_date, appliance, tariff, kwh) VALUES (?, ?, ?, ?)",
                [(r["local_date"], r["appliance"], tariff, r["kwh"]) for r in rows],
            )
            self._conn.commit()

    def bill_prices(self, year: int) -> dict[tuple[str, str], float]:
        return {
            (r["month"], r["tariff"]): r["price"]
            for r in self._query("SELECT month, tariff, price FROM bill_prices WHERE month LIKE ?", (f"{year:04d}-%",))
        }

    def bill_rows(self, first_date: str, last_date: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT local_date, tariff, kwh, cost FROM bill_days WHERE local_date >= ? AND local_date <= ? "
            "ORDER BY local_date, tariff",
            (first_date, last_date),
        )

    def bill_years(self) -> list[int]:
        return [int(r["y"]) for r in self._query("SELECT DISTINCT substr(local_date, 1, 4) AS y FROM bill_days ORDER BY y")]

    # Analyses -----------------------------------------------------------

    def start_analysis(self, trigger: str, model: str) -> int:
        cursor = self._execute(
            "INSERT INTO analyses (ts, trigger, model, status) VALUES (?, ?, ?, 'running')",
            (int(time.time()), trigger, model),
        )
        return int(cursor.lastrowid)

    def finish_analysis(
        self,
        analysis_id: int,
        *,
        status: str,
        model: str | None = None,
        summary: str | None = None,
        result: dict[str, Any] | None = None,
        input_data: dict[str, Any] | None = None,
        error: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self._execute(
            """UPDATE analyses SET finished_ts = ?, status = ?, model = COALESCE(?, model), summary = ?,
                   result_json = ?, input_json = ?, error = ?, input_tokens = ?, output_tokens = ?
               WHERE id = ?""",
            (
                int(time.time()),
                status,
                model,
                summary,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                json.dumps(input_data, default=str, ensure_ascii=False) if input_data is not None else None,
                error,
                input_tokens,
                output_tokens,
                analysis_id,
            ),
        )

    def add_actions(self, analysis_id: int, actions: list[dict[str, Any]]) -> None:
        """Appends inverter changes made for this analysis (auto-apply or the Apply button)."""
        rows = self._query("SELECT actions_json FROM analyses WHERE id = ?", (analysis_id,))
        if not rows:
            return
        existing = json.loads(rows[0]["actions_json"] or "[]")
        self._execute(
            "UPDATE analyses SET actions_json = ? WHERE id = ?",
            (json.dumps(existing + actions, ensure_ascii=False), analysis_id),
        )

    def analyses(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT id, ts, finished_ts, trigger, model, status, summary, result_json, error,
                      input_tokens, output_tokens, actions_json
               FROM analyses ORDER BY ts DESC, id DESC LIMIT ?""",
            (limit,),
        )
        for row in rows:
            row["result"] = json.loads(row.pop("result_json") or "null")
            row["actions"] = json.loads(row.pop("actions_json") or "[]")
        return rows

    def analysis(self, analysis_id: int) -> dict[str, Any] | None:
        return next((a for a in self.analyses(500) if a["id"] == analysis_id), None)

    def ok_analyses_since(self, since_ts: int) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT id, ts, result_json FROM analyses WHERE status = 'ok' AND ts >= ? ORDER BY ts",
            (since_ts,),
        )
        for row in rows:
            row["result"] = json.loads(row.pop("result_json") or "null") or {}
        return rows

    def analysis_input(self, analysis_id: int) -> Any | None:
        rows = self._query("SELECT input_json FROM analyses WHERE id = ?", (analysis_id,))
        if not rows:
            return None
        return json.loads(rows[0]["input_json"] or "null")

    def mark_interrupted(self) -> None:
        """Analyses still 'running' at startup were cut off by a restart."""
        self._execute(
            "UPDATE analyses SET status = 'error', error = 'Interrupted by add-on restart' WHERE status = 'running'"
        )


def _encode(field: str, value: Any) -> Any:
    if field == "appliances":
        return json.dumps(value) if value else None
    return value


def _decode_appliances(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        raw = row.get("appliances")
        row["appliances"] = json.loads(raw) if isinstance(raw, str) and raw else {}
    return rows


def accuracy_report(db: Database, days: int, today: date, tz: tzinfo) -> dict[str, Any]:
    """Compares predictions with what happened, per day.

    The prediction for day D is the tomorrow-consumption from the last analysis made on D-1.
    """
    summary = [d for d in db.daily_summary(days + 1, today) if d["date"] != today.isoformat()]
    first_ts = int(datetime.combine(today - timedelta(days=days + 2), datetime.min.time(), tz).timestamp())
    predictions: dict[str, dict[str, Any]] = {}
    for analysis in db.ok_analyses_since(first_ts):
        made_on = datetime.fromtimestamp(analysis["ts"], tz).date()
        predictions[(made_on + timedelta(days=1)).isoformat()] = analysis  # later runs overwrite

    out = []
    for day in summary[-days:]:
        actual = day["consumption_kwh"]
        analysis = predictions.get(day["date"])
        predicted = analysis["result"].get("predicted_consumption_tomorrow_kwh") if analysis else None
        pv, forecast = day["pv_kwh"], day["solar_forecast_kwh"]
        out.append(
            {
                "date": day["date"],
                "weekday": day["weekday"],
                "is_weekend": day["is_weekend"],
                "predicted_kwh": predicted,
                "actual_kwh": actual,
                "prediction_accuracy": _accuracy(predicted, actual),
                "solar_forecast_kwh": forecast,
                "pv_kwh": pv,
                "solar_accuracy": _accuracy(forecast, pv),
                "pv_coverage": _percent(min(pv, actual) if pv is not None and actual else None, actual),
                "grid_import_kwh": day["grid_import_kwh"],
                "grid_share": _percent(day["grid_import_kwh"], actual),
                "temp_avg": day["temp_avg"],
            }
        )

    def mean(key: str) -> float | None:
        values = [row[key] for row in out if row[key] is not None]
        return round(sum(values) / len(values), 1) if values else None

    return {
        "days": out,
        "average": {
            key: mean(key) for key in ("prediction_accuracy", "solar_accuracy", "pv_coverage", "grid_share")
        },
    }


def _accuracy(predicted: float | None, actual: float | None) -> float | None:
    if predicted is None or not actual:
        return None
    return round(max(0.0, 100 - abs(predicted - actual) / actual * 100), 1)


def _percent(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def _max(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    return value if current is None else max(current, value)


def economy_report(
    db: Database, days: int, today: date, tariff_at: Callable[[int], tuple[float, str]]
) -> dict[str, Any]:
    """Per day: cost without PV/battery vs. what was paid, and where the savings came from.

    - without_system: the whole load bought from the grid at the tariff of its time.
    - pv_saved: load covered directly by PV power at that moment.
    - paid: grid import (counter deltas) at the tariff of its time.
    - battery_saved: everything else saved - battery discharge (from PV or cheap off-peak
      grid charging planned by the predictions) replacing grid energy at peak times.
    """
    first = (today - timedelta(days=days - 1)).isoformat()
    rows = db._query(
        """SELECT ts, local_date, weekday, is_weekend, minute_of_day, load_power, pv_power,
                  grid_import_today, control_mode, resolution_s
           FROM readings WHERE local_date >= ? ORDER BY ts""",
        (first,),
    )
    out: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        day = out.get(row["local_date"])
        if day is None:
            day = out[row["local_date"]] = {
                "date": row["local_date"],
                "weekday": row["weekday"],
                "is_weekend": bool(row["is_weekend"]),
                "load_kwh": 0.0,
                "without_system": 0.0,
                "pv_direct_kwh": 0.0,
                "pv_saved": 0.0,
                "grid_kwh": 0.0,
                "grid_by_tariff": {},  # tariff name -> kWh bought
                "paid": 0.0,
                "_grid_samples": 0,
                "_auto": 0,
                "_samples": 0,
            }
        day["_samples"] += 1
        day["_auto"] += row["control_mode"] == "auto"
        if (
            day["_samples"] == 1
            and (row["resolution_s"] or 0) >= HOURLY
            and row["minute_of_day"] < 60
            and row["grid_import_today"] is not None
        ):
            # The day's first hourly row already includes what was bought since the midnight reset.
            price0, zone0 = tariff_at(row["minute_of_day"])
            day["grid_kwh"] += row["grid_import_today"]
            day["grid_by_tariff"][zone0] = day["grid_by_tariff"].get(zone0, 0.0) + row["grid_import_today"]
            day["paid"] += row["grid_import_today"] * price0
            day["_grid_samples"] += 1
        if index + 1 >= len(rows):
            continue
        following = rows[index + 1]
        gap = min(following["ts"] - row["ts"], max(MAX_INTEGRATION_GAP, row["resolution_s"] or 0))
        price, zone = tariff_at(row["minute_of_day"])
        if row["load_power"] is not None:
            kwh = row["load_power"] * gap / 3_600_000
            day["load_kwh"] += kwh
            day["without_system"] += kwh * price
            if row["pv_power"] is not None:
                direct = min(row["load_power"], row["pv_power"]) * gap / 3_600_000
                day["pv_direct_kwh"] += direct
                day["pv_saved"] += direct * price
        if (
            following["local_date"] == row["local_date"]
            and row["grid_import_today"] is not None
            and following["grid_import_today"] is not None
        ):
            delta = following["grid_import_today"] - row["grid_import_today"]
            if delta >= 0:  # a negative step is the midnight reset
                # Hourly rows hold the counter at the end of their hour, so the step up to the
                # next row was bought during the next row's hour.
                grid_price, grid_zone = (
                    tariff_at(following["minute_of_day"]) if (row["resolution_s"] or 0) >= HOURLY else (price, zone)
                )
                day["grid_kwh"] += delta
                day["grid_by_tariff"][grid_zone] = day["grid_by_tariff"].get(grid_zone, 0.0) + delta
                day["paid"] += delta * grid_price
                day["_grid_samples"] += 1

    result = []
    for day in out.values():
        has_grid = day.pop("_grid_samples") > 0
        samples = day.pop("_samples")
        day["ai_control_share"] = round(day.pop("_auto") / samples * 100) if samples else 0
        if not has_grid:
            day["paid"] = day["grid_kwh"] = None
            day["grid_by_tariff"] = {}
        day["total_saved"] = day["without_system"] - day["paid"] if has_grid else None
        day["battery_saved"] = day["total_saved"] - day["pv_saved"] if has_grid else None
        day["saved_percent"] = (
            round(day["total_saved"] / day["without_system"] * 100, 1)
            if has_grid and day["without_system"]
            else None
        )
        for key, value in list(day.items()):
            if isinstance(value, float):
                day[key] = round(value, 2)
        day["grid_by_tariff"] = {k: round(v, 2) for k, v in day["grid_by_tariff"].items()}
        result.append(day)

    def total(key: str) -> float | None:
        values = [d[key] for d in result if d[key] is not None]
        return round(sum(values), 2) if values else None

    totals = {key: total(key) for key in (
        "load_kwh", "without_system", "paid", "pv_saved", "battery_saved", "total_saved",
        "grid_kwh", "pv_direct_kwh",
    )}
    by_tariff: dict[str, float] = {}
    for day in result:
        for name, kwh in day["grid_by_tariff"].items():
            by_tariff[name] = round(by_tariff.get(name, 0.0) + kwh, 2)
    totals["grid_by_tariff"] = by_tariff
    totals["saved_percent"] = (
        round(totals["total_saved"] / totals["without_system"] * 100, 1)
        if totals["total_saved"] is not None and totals["without_system"]
        else None
    )
    return {"days": result, "totals": totals}


def all_days(db: Database, today: date) -> int:
    """Number of days back to the first stored reading (at least 1)."""
    first = db.earliest_ts()
    if first is None:
        return 1
    return max(1, (today - datetime.fromtimestamp(first).date()).days + 2)


def monthly_summary(db: Database, today: date, tariff_at: Callable[[int], tuple[float, str]] | None = None) -> list[dict[str, Any]]:
    """One row per calendar month over all stored history: energy, PV, appliances,
    temperature and (with tariff_at) what was paid and saved."""
    days = all_days(db, today)
    daily = db.daily_summary(days, today)
    economy = {d["date"]: d for d in economy_report(db, days, today, tariff_at)["days"]} if tariff_at else {}
    months: dict[str, dict[str, Any]] = {}
    for day in daily:
        month = months.setdefault(day["date"][:7], {
            "month": day["date"][:7], "days": 0, "consumption_kwh": 0.0, "pv_kwh": 0.0, "grid_import_kwh": 0.0,
            "_temps": [], "_weekday": [], "_weekend": [], "appliances_kwh": {},
            "paid": None, "without_system": None, "total_saved": None, "pv_saved": None,
        })
        month["days"] += 1
        for key in ("consumption_kwh", "pv_kwh", "grid_import_kwh"):
            month[key] += day[key] or 0
        if day["temp_avg"] is not None:
            month["_temps"].append(day["temp_avg"])
        if day["consumption_kwh"]:
            month["_weekend" if day["is_weekend"] else "_weekday"].append(day["consumption_kwh"])
        for app_id, entry in day["appliances"].items():
            month["appliances_kwh"][app_id] = month["appliances_kwh"].get(app_id, 0.0) + entry["kwh"]
        money = economy.get(day["date"])
        if money and money["paid"] is not None:
            for key in ("paid", "without_system", "total_saved", "pv_saved"):
                month[key] = (month[key] or 0.0) + (money[key] or 0.0)

    out = []
    for month in months.values():
        temps, weekday, weekend = month.pop("_temps"), month.pop("_weekday"), month.pop("_weekend")
        month["temp_avg"] = round(sum(temps) / len(temps), 1) if temps else None
        month["avg_weekday_kwh"] = round(sum(weekday) / len(weekday), 1) if weekday else None
        month["avg_weekend_kwh"] = round(sum(weekend) / len(weekend), 1) if weekend else None
        month["avg_daily_kwh"] = round(month["consumption_kwh"] / month["days"], 1) if month["days"] else None
        month["avg_daily_pv_kwh"] = round(month["pv_kwh"] / month["days"], 1) if month["days"] else None
        for key in ("consumption_kwh", "pv_kwh", "grid_import_kwh", "paid", "without_system", "total_saved", "pv_saved"):
            if month[key] is not None:
                month[key] = round(month[key], 2)
        month["appliances_kwh"] = {k: round(v, 1) for k, v in month["appliances_kwh"].items()}
        month["saved_percent"] = (
            round(month["total_saved"] / month["without_system"] * 100, 1)
            if month["total_saved"] is not None and month["without_system"] else None
        )
        out.append(month)
    return out
