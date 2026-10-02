"""Reads the configured Home Assistant entities into one snapshot."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from config import WEEKDAYS, Options
from ha import HAError, HomeAssistant

UNAVAILABLE = {"", "unknown", "unavailable", "none", "null"}
MAX_ATTRIBUTES_CHARS = 4000
POWER_FIELDS = ("load_power", "pv_power")
NUMERIC_FIELDS = (
    "battery_soc",
    "today_forecast",
    "tomorrow_forecast",
    "today_consumption",
    *POWER_FIELDS,
    "pv_today",
    "grid_import_today",
    "outdoor_temp",
)
FORECAST_CACHE_SECONDS = 1800
_forecast_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def to_float(value: Any) -> float | None:
    if value is None or str(value).strip().lower() in UNAVAILABLE:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_watts(value: float | None, unit: str | None) -> float | None:
    """Power sensors may report W or kW; everything is stored in W."""
    if value is None:
        return None
    return value * 1000 if (unit or "").strip().lower() == "kw" else value


def clean_state(value: Any) -> str | None:
    if value is None or str(value).strip().lower() in UNAVAILABLE:
        return None
    return str(value)


def parse_hhmm(value: Any) -> int | None:
    """Minutes after midnight from '01:00', '01:00:00', '1:00' or Deye-style 100 / '0100'."""
    text = clean_state(value)
    if text is None:
        return None
    try:
        if ":" in text:
            hour, minute = text.split(":")[:2]
            hours, minutes = int(hour), int(float(minute))
        else:
            number = int(float(text))
            hours, minutes = divmod(number, 100)
    except ValueError:
        return None
    if 0 <= hours < 24 and 0 <= minutes < 60:
        return hours * 60 + minutes
    return None


def _fmt_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def program_ranges(programs: list[dict[str, Any]], time_is_end: bool) -> dict[int, tuple[int, int]]:
    """slot -> (start, end) minutes of the period each program covers, wrapping past midnight.

    time_is_end: the program's time marks the END of its period, which starts at the previous
    program's time (P1 05:00 covers 23:15-05:00 when the last program is at 23:15).
    Otherwise the time marks the START and the period runs until the next program's time.
    """
    timed = sorted(
        ((parse_hhmm(p.get("time")), p["slot"]) for p in programs if parse_hhmm(p.get("time")) is not None),
    )
    ranges = {}
    for index, (minute, slot) in enumerate(timed):
        if time_is_end:
            ranges[slot] = (timed[index - 1][0], minute)
        else:
            ranges[slot] = (minute, timed[(index + 1) % len(timed)][0])
    return ranges


def _in_range(minute: int, start: int, end: int) -> bool:
    if start == end:
        return False
    return start <= minute < end if start < end else minute >= start or minute < end


def active_program(
    programs: list[dict[str, Any]], minute_of_day: int, time_is_end: bool = True
) -> dict[str, Any] | None:
    """The Deye program in effect at a minute of the day."""
    ranges = program_ranges(programs, time_is_end)
    for program in programs:
        span = ranges.get(program["slot"])
        if span and _in_range(minute_of_day, *span):
            return program
    return None


def annotate_ranges(programs: list[dict[str, Any]], time_is_end: bool) -> None:
    """Adds a readable "range" (e.g. "23:15-05:00") to every program."""
    ranges = program_ranges(programs, time_is_end)
    for program in programs:
        span = ranges.get(program["slot"])
        program["range"] = f"{_fmt_minutes(span[0])}-{_fmt_minutes(span[1])}" if span else None


def _trim_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    text = json.dumps(attributes, default=str, ensure_ascii=False)
    if len(text) <= MAX_ATTRIBUTES_CHARS:
        return attributes
    return {"truncated_json": text[:MAX_ATTRIBUTES_CHARS]}


def _forecast_date(entry: dict[str, Any], tz: tzinfo) -> date | None:
    try:
        return datetime.fromisoformat(str(entry.get("datetime"))).astimezone(tz).date()
    except ValueError:
        return None


def _day_summary(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    keys = ("condition", "temperature", "templow", "precipitation", "precipitation_probability",
            "wind_speed", "cloud_coverage", "humidity")
    return {key: entry[key] for key in keys if entry.get(key) is not None}


async def weather_details(ha: HomeAssistant, entity_id: str, tz: tzinfo) -> dict[str, Any]:
    """Current outdoor temperature plus today's/tomorrow's forecast for a weather or sensor entity."""
    state = await ha.state(entity_id)
    if state is None:
        return {}
    attributes = state.get("attributes") or {}
    if not entity_id.startswith("weather."):
        # A plain temperature sensor: current value only, no forecast.
        return {"outdoor_temp": to_float(state.get("state")), "unit": attributes.get("unit_of_measurement")}

    details: dict[str, Any] = {
        "outdoor_temp": to_float(attributes.get("temperature")),
        "unit": attributes.get("temperature_unit"),
        "condition": clean_state(state.get("state")),
    }
    cached = _forecast_cache.get(entity_id)
    if cached and time.time() - cached[0] < FORECAST_CACHE_SECONDS:
        details.update(cached[1])
        return details

    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    forecast: dict[str, Any] = {}
    try:
        daily = await ha.weather_forecast(entity_id, "daily")
        by_date = {_forecast_date(entry, tz): entry for entry in daily}
        forecast["today"] = _day_summary(by_date.get(today))
        forecast["tomorrow"] = _day_summary(by_date.get(tomorrow))
    except HAError as err:
        forecast["forecast_error"] = str(err)
    try:
        hourly = await ha.weather_forecast(entity_id, "hourly")
        forecast["tomorrow_hourly"] = [
            {
                "hour": datetime.fromisoformat(str(entry["datetime"])).astimezone(tz).hour,
                "temperature": entry.get("temperature"),
                "condition": entry.get("condition"),
            }
            for entry in hourly
            if _forecast_date(entry, tz) == tomorrow
        ]
    except (HAError, KeyError, ValueError):
        forecast["tomorrow_hourly"] = []  # many weather integrations only offer daily forecasts
    _forecast_cache[entity_id] = (time.time(), forecast)
    details.update(forecast)
    return details


async def collect(ha: HomeAssistant, opts: Options, tz: tzinfo) -> dict[str, Any]:
    now = datetime.now(tz)
    sensors = opts.sensor_map()
    program_entities = [(p.time_entity, p.soc_entity, p.charge_entity) for p in opts.deye_programs]

    sensor_states, appliance_states, program_states, weather = await asyncio.gather(
        asyncio.gather(*(ha.state(entity) for entity in sensors.values())),
        asyncio.gather(*(ha.state(a.entity) for a in opts.appliances)),
        asyncio.gather(*(asyncio.gather(ha.state(t), ha.state(s), ha.state(c)) for t, s, c in program_entities)),
        weather_details(ha, opts.weather_entity, tz) if opts.weather_entity else asyncio.sleep(0, {}),
    )
    by_field = dict(zip(sensors.keys(), sensor_states))

    missing = [
        entity
        for name, entity in sensors.items()
        if entity and (by_field[name] is None or clean_state(by_field[name].get("state")) is None)
    ]
    if opts.weather_entity and weather.get("outdoor_temp") is None:
        missing.append(opts.weather_entity)

    snapshot: dict[str, Any] = {
        "ts": int(now.timestamp()),
        "local_time": now.isoformat(timespec="minutes"),
        "local_date": now.date().isoformat(),
        "weekday": WEEKDAYS[now.weekday()],
        "is_weekend": WEEKDAYS[now.weekday()] in opts.weekend_days,
        "units": {},
    }
    for name in NUMERIC_FIELDS:
        if name == "outdoor_temp":
            continue
        state = by_field.get(name)
        value = to_float(state.get("state")) if state else None
        unit = (state.get("attributes") or {}).get("unit_of_measurement") if state else None
        if name in POWER_FIELDS:
            value, unit = to_watts(value, unit), "W"
        snapshot[name] = value
        if state:
            snapshot["units"][name] = unit
    snapshot["appliances"] = {}
    for appliance, state in zip(opts.appliances, appliance_states):
        if not appliance.entity:
            continue
        unit = (state.get("attributes") or {}).get("unit_of_measurement") if state else None
        value = to_watts(to_float(state.get("state")) if state else None, unit)
        snapshot["appliances"][appliance.id] = value
        if value is None:
            missing.append(appliance.entity)
    snapshot["outdoor_temp"] = weather.get("outdoor_temp")
    snapshot["units"]["outdoor_temp"] = weather.get("unit")
    snapshot["weather"] = weather

    outages = by_field.get("outages")
    snapshot["outages_state"] = clean_state(outages.get("state")) if outages else None
    snapshot["outages_attrs"] = _trim_attributes(outages.get("attributes")) if outages else {}

    programs = []
    for program, (time_state, soc_state, charge_state) in zip(opts.deye_programs, program_states):
        time_value = clean_state(time_state.get("state")) if time_state else None
        soc_value = to_float(soc_state.get("state")) if soc_state else None
        charge_value = clean_state(charge_state.get("state")) if charge_state else None
        if program.time_entity and time_value is None:
            missing.append(program.time_entity)
        if program.soc_entity and soc_value is None:
            missing.append(program.soc_entity)
        entry: dict[str, Any] = {"slot": program.slot, "time": time_value, "soc": soc_value}
        if program.charge_entity:
            entry["grid_charge"] = charge_value
        programs.append(entry)
    snapshot["deye_programs"] = programs

    time_is_end = opts.program_time_marks == "end"
    annotate_ranges(programs, time_is_end)
    current = active_program(programs, now.hour * 60 + now.minute, time_is_end)
    snapshot["active_program_slot"] = current["slot"] if current else None
    snapshot["missing_entities"] = missing
    return snapshot


def has_data(snapshot: dict[str, Any]) -> bool:
    return any(snapshot.get(name) is not None for name in NUMERIC_FIELDS) or any(
        p["time"] is not None or p["soc"] is not None for p in snapshot.get("deye_programs", [])
    )
