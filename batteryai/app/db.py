"""SQLite storage for sensor readings and AI analyses."""

from __future__ import annotations

import json
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
}
NEW_ANALYSIS_COLUMNS = {"actions_json": "TEXT"}

READING_FIELDS = (
    "battery_soc",
    "today_forecast",
    "tomorrow_forecast",
    "today_consumption",
    "load_power",
    "heat_pump_power",
    "boiler_power",
    "ev_power",
    "outdoor_temp",
    "pv_today",
    "grid_import_today",
    "pv_power",
    "control_mode",
)
APPLIANCES = ("load_power", "heat_pump_power", "boiler_power", "ev_power")

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
            *(snap.get(field) for field in READING_FIELDS),
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
        self._execute(self._INSERT, self._reading_values(snap, tz, "live"))

    def replace_range(self, start_ts: int, end_ts: int, snaps: list[dict[str, Any]], tz: tzinfo) -> None:
        """Replaces all readings in [start_ts, end_ts) with imported history."""
        with self._lock:
            self._conn.execute("DELETE FROM readings WHERE ts >= ? AND ts < ?", (start_ts, end_ts))
            self._conn.executemany(self._INSERT, [self._reading_values(s, tz, "history") for s in snaps])
            self._conn.commit()

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
        return self._query(
            "SELECT ts, local_date, minute_of_day, weekday, is_weekend, "
            + ", ".join(READING_FIELDS)
            + ", outages_state, target_soc FROM readings WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        )

    def latest_reading(self) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM readings ORDER BY ts DESC LIMIT 1")
        if not rows:
            return None
        row = rows[0]
        row["deye_programs"] = json.loads(row["deye_programs"] or "[]")
        row["outages_attrs"] = json.loads(row["outages_attrs"] or "{}")
        return row

    def reading_count(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM readings")[0]["n"]

    def earliest_ts(self) -> int | None:
        return self._query("SELECT MIN(ts) AS ts FROM readings")[0]["ts"]

    def daily_summary(self, days: int, today: date) -> list[dict[str, Any]]:
        first = (today - timedelta(days=days - 1)).isoformat()
        rows = self._query(
            "SELECT ts, local_date, minute_of_day, weekday, is_weekend, "
            + ", ".join(READING_FIELDS)
            + ", outages_state FROM readings WHERE local_date >= ? ORDER BY ts",
            (first,),
        )
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
                    **{f"{name.removesuffix('_power')}_kwh": 0.0 for name in APPLIANCES},
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
                gap = min(rows[index + 1]["ts"] - row["ts"], MAX_INTEGRATION_GAP)
                hour = row["minute_of_day"] // 60
                for name in APPLIANCES:
                    if row[name] is not None:
                        day[f"{name.removesuffix('_power')}_kwh"] += row[name] * gap / 3_600_000
                        hourly_power.setdefault((row["local_date"], name, hour), []).append(row[name])

            state = row["outages_state"]
            if state and state not in day["outage_states"] and len(day["outage_states"]) < 10:
                day["outage_states"].append(state)

        for day in days_out.values():
            temps = day.pop("_temps")
            day["temp_avg"] = round(sum(temps) / len(temps), 1) if temps else None
            for name in APPLIANCES:
                key = f"{name.removesuffix('_power')}_kwh"
                day[key] = round(day[key], 2)
                active = sorted(
                    hour
                    for (date_key, appliance, hour), values in hourly_power.items()
                    if date_key == day["date"] and appliance == name and sum(values) / len(values) >= ACTIVE_POWER_W
                )
                if name != "load_power":
                    day[f"{name.removesuffix('_power')}_active_hours"] = active
        return list(days_out.values())

    def hourly_profile(self, since_ts: int) -> list[dict[str, Any]]:
        """Average power per hour of day, split into weekdays and weekends."""
        rows = self._query(
            "SELECT is_weekend, minute_of_day / 60 AS hour, "
            + ", ".join(f"AVG({name}) AS {name}" for name in APPLIANCES)
            + ", AVG(pv_power) AS pv_power, AVG(outdoor_temp) AS outdoor_temp FROM readings WHERE ts >= ? "
            "GROUP BY is_weekend, hour ORDER BY is_weekend, hour",
            (since_ts,),
        )
        return [
            {key: (round(value, 1) if isinstance(value, float) else value) for key, value in row.items()}
            for row in rows
        ]

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
                  grid_import_today, control_mode
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
                "grid_peak_kwh": 0.0,
                "grid_offpeak_kwh": 0.0,
                "paid": 0.0,
                "_grid_samples": 0,
                "_auto": 0,
                "_samples": 0,
            }
        day["_samples"] += 1
        day["_auto"] += row["control_mode"] == "auto"
        if index + 1 >= len(rows):
            continue
        following = rows[index + 1]
        gap = min(following["ts"] - row["ts"], MAX_INTEGRATION_GAP)
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
                day["grid_kwh"] += delta
                day[f"grid_{zone}_kwh"] += delta
                day["paid"] += delta * price
                day["_grid_samples"] += 1

    result = []
    for day in out.values():
        has_grid = day.pop("_grid_samples") > 0
        samples = day.pop("_samples")
        day["ai_control_share"] = round(day.pop("_auto") / samples * 100) if samples else 0
        if not has_grid:
            day["paid"] = day["grid_kwh"] = day["grid_peak_kwh"] = day["grid_offpeak_kwh"] = None
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
        result.append(day)

    def total(key: str) -> float | None:
        values = [d[key] for d in result if d[key] is not None]
        return round(sum(values), 2) if values else None

    totals = {key: total(key) for key in (
        "load_kwh", "without_system", "paid", "pv_saved", "battery_saved", "total_saved",
        "grid_kwh", "grid_peak_kwh", "grid_offpeak_kwh", "pv_direct_kwh",
    )}
    totals["saved_percent"] = (
        round(totals["total_saved"] / totals["without_system"] * 100, 1)
        if totals["total_saved"] is not None and totals["without_system"]
        else None
    )
    return {"days": result, "totals": totals}
