"""Shared planning rules for every prediction engine.

- Plan day: a prediction run before PLAN_TODAY_BEFORE_HOUR plans today (its programs are
  still ahead), a later run plans tomorrow.
- Night reserve: when outages are likely (an outage is scheduled or emergency outages are
  on now, or outages were recorded in the last days), the programs that run in the evening
  and night – when there is no PV – are kept at the night reserve SOC, so the battery is
  full when the sun is gone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

from collector import program_ranges
from config import WEEKDAYS, Options

PLAN_TODAY_BEFORE_HOUR = 13
OUTAGE_LOOKBACK_DAYS = 7
NIGHT_PV_W = 100  # an hour with less average PV than this is night
DEFAULT_NIGHT = set(range(18, 24)) | set(range(0, 7))


def plan_day(snapshot: dict[str, Any], opts: Options, tz: tzinfo) -> dict[str, Any]:
    now = datetime.fromtimestamp(snapshot["ts"], tz)
    is_today = now.hour < PLAN_TODAY_BEFORE_HOUR
    day = now.date() if is_today else (now + timedelta(days=1)).date()
    weekday = WEEKDAYS[day.weekday()]
    weather = snapshot.get("weather") or {}
    return {
        "date": day.isoformat(),
        "label": "today" if is_today else "tomorrow",
        "weekday": weekday,
        "is_weekend": weekday in opts.weekend_days,
        "solar_forecast_kwh": snapshot.get("today_forecast" if is_today else "tomorrow_forecast"),
        "weather": weather.get("today" if is_today else "tomorrow"),
    }


def night_hours(profile: list[dict[str, Any]]) -> set[int]:
    """Hours of the day without useful PV, from the recorded average PV power per hour."""
    pv: dict[int, list[float]] = {}
    for row in profile:
        if row.get("pv_power") is not None:
            pv.setdefault(row["hour"], []).append(row["pv_power"])
    if len(pv) < 20:
        return set(DEFAULT_NIGHT)
    return {h for h in range(24) if sum(pv.get(h, [0])) / len(pv.get(h, [0])) < NIGHT_PV_W}


def recent_outage_days(db: Any, now_ts: int) -> list[str]:
    """Days in the last week on which an outage was scheduled or emergency outages were on."""
    since = now_ts - OUTAGE_LOOKBACK_DAYS * 86400
    rows = db._query(
        "SELECT DISTINCT local_date FROM readings WHERE ts >= ? AND lower(outages_state) IN ('on', 'emergency') "
        "ORDER BY local_date",
        (since,),
    )
    return [r["local_date"] for r in rows]


def outages_likely(snapshot: dict[str, Any], outage_days: list[str]) -> str | None:
    """Why outages are likely tonight, or None."""
    attrs = snapshot.get("outages_attrs") or {}
    if attrs.get("emergency_outages"):
        return "emergency outages are on"
    if attrs.get("scheduled_outage"):
        return f"an outage is scheduled ({attrs.get('outage_starts') or 'soon'})"
    if outage_days:
        return f"outages on {len(outage_days)} of the last {OUTAGE_LOOKBACK_DAYS} days"
    return None


def apply_night_reserve(
    result: dict[str, Any], opts: Options, snapshot: dict[str, Any], night: set[int], reason: str | None,
    keep_grid_charge: bool,
) -> list[int]:
    """Evening/night programs (mostly hours without PV):
    - grid charge always stays on (unless grid charge is left alone), so the battery can
      recharge before an unplanned emergency outage – a prediction never turns it off there;
    - when outages are likely (reason), their SOC is raised to the night reserve.
    Returns the slots it changed."""
    reserve = min(opts.night_reserve_soc_percent, opts.max_soc_percent) if reason else 0
    times = {p["slot"]: p.get("time") for p in snapshot.get("deye_programs") or []}
    programs = [
        {**p, "time": times.get(p.get("slot")) or p.get("time")}
        for p in result.get("deye_programs") or [] if isinstance(p, dict) and p.get("slot") is not None
    ]
    ranges = program_ranges(programs, opts.program_time_marks == "end")
    changed, raised = [], []
    for program in result.get("deye_programs") or []:
        span = ranges.get(program.get("slot"))
        if not span or span[0] == span[1]:
            continue
        start, end = span
        hours = {(m // 60) % 24 for m in range(start, end if end > start else end + 1440, 60)}
        if not hours or len(hours & night) * 2 < len(hours):
            continue  # mostly daytime: PV charges the battery there
        notes = []
        if reserve and (program.get("soc_percent") is None or program["soc_percent"] < reserve):
            program["soc_percent"] = reserve
            notes.append(f"Night reserve {reserve}%: {reason}.")
            raised.append(program["slot"])
        if not keep_grid_charge and program.get("grid_charge") is False:
            program["grid_charge"] = True
            notes.append("Grid charge stays on at night, so the battery can recharge for an unplanned outage.")
        if notes:
            program["reason"] = f"{program.get('reason') or ''} {' '.join(notes)}".strip()
            changed.append(program["slot"])
    if raised:
        slots = ", ".join(f"P{s}" for s in raised)
        result["summary"] = f"{result.get('summary') or ''} Night reserve: {slots} at {reserve}% ({reason}).".strip()
    return changed
