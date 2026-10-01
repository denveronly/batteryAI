"""SQLite storage for sensor readings and AI analyses."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

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

# Daily energy counters may still show yesterday's total for a few minutes after midnight.
DAILY_RESET_GRACE_MINUTES = 15


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # Readings -----------------------------------------------------------

    def add_reading(self, snap: dict[str, Any], tz: tzinfo) -> None:
        local = datetime.fromtimestamp(snap["ts"], tz)
        active = next(
            (p for p in snap["deye_programs"] if p["slot"] == snap.get("active_program_slot")), None
        )
        self._execute(
            """INSERT INTO readings (ts, local_date, minute_of_day, weekday, is_weekend,
                   battery_soc, today_forecast, tomorrow_forecast, today_load, today_consumption,
                   outages_state, outages_attrs, deye_programs, target_soc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snap["ts"],
                snap["local_date"],
                local.hour * 60 + local.minute,
                snap["weekday"],
                int(snap["is_weekend"]),
                snap["battery_soc"],
                snap["today_forecast"],
                snap["tomorrow_forecast"],
                snap["today_load"],
                snap["today_consumption"],
                snap["outages_state"],
                json.dumps(snap["outages_attrs"], default=str, ensure_ascii=False),
                json.dumps(snap["deye_programs"]),
                active["soc"] if active else None,
            ),
        )

    def readings_since(self, since_ts: int) -> list[dict[str, Any]]:
        return self._query(
            """SELECT ts, local_date, minute_of_day, weekday, is_weekend, battery_soc,
                      today_forecast, tomorrow_forecast, today_load, today_consumption,
                      outages_state, target_soc
               FROM readings WHERE ts >= ? ORDER BY ts""",
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

    def daily_summary(self, days: int, today: date) -> list[dict[str, Any]]:
        first = (today - timedelta(days=days - 1)).isoformat()
        rows = self._query(
            """SELECT local_date, minute_of_day, weekday, is_weekend, battery_soc, today_forecast,
                      tomorrow_forecast, today_load, today_consumption, outages_state
               FROM readings WHERE local_date >= ? ORDER BY ts""",
            (first,),
        )
        days_out: dict[str, dict[str, Any]] = {}
        for row in rows:
            day = days_out.setdefault(
                row["local_date"],
                {
                    "date": row["local_date"],
                    "weekday": row["weekday"],
                    "is_weekend": bool(row["is_weekend"]),
                    "load_kwh": None,
                    "consumption_kwh": None,
                    "solar_forecast_kwh": None,
                    "min_soc": None,
                    "max_soc": None,
                    "outage_states": [],
                    "samples": 0,
                },
            )
            day["samples"] += 1
            if row["minute_of_day"] >= DAILY_RESET_GRACE_MINUTES:
                day["load_kwh"] = _max(day["load_kwh"], row["today_load"])
                day["consumption_kwh"] = _max(day["consumption_kwh"], row["today_consumption"])
            if row["today_forecast"] is not None:
                day["solar_forecast_kwh"] = row["today_forecast"]  # latest forecast of the day
            soc = row["battery_soc"]
            if soc is not None:
                day["min_soc"] = soc if day["min_soc"] is None else min(day["min_soc"], soc)
                day["max_soc"] = _max(day["max_soc"], soc)
            state = row["outages_state"]
            if state and state not in day["outage_states"] and len(day["outage_states"]) < 10:
                day["outage_states"].append(state)
        return list(days_out.values())

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

    def analyses(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT id, ts, finished_ts, trigger, model, status, summary, result_json, error,
                      input_tokens, output_tokens
               FROM analyses ORDER BY ts DESC, id DESC LIMIT ?""",
            (limit,),
        )
        for row in rows:
            row["result"] = json.loads(row.pop("result_json") or "null")
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


def _max(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    return value if current is None else max(current, value)
