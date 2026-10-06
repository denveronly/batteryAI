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


ON_STATES = {"on", "true", "yes", "1", "active", "emergency", "увімкнено", "так"}
# "Minutes to outage" at or above this means no outage is scheduled (svitlo shows 9999).
FAR_MINUTES = 9999


def is_on(state: dict[str, Any] | None) -> bool:
    """An on/off indicator (e.g. emergency outages): on/true/yes/active, or a number above 0."""
    if not state:
        return False
    raw = str(state.get("state") or "").strip().lower()
    number = to_float(raw)
    return raw in ON_STATES or (number is not None and number > 0)


def outage_minutes(state: dict[str, Any] | None) -> float | None:
    """Minutes until the next scheduled outage (h and s are converted); None when unknown
    or when no outage is scheduled (9999 or more)."""
    if not state:
        return None
    value = to_float(state.get("state"))
    if value is None:
        return None
    unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "").strip().lower()
    value = value * 60 if unit in ("h", "hours") else value / 60 if unit in ("s", "sec", "seconds") else value
    return None if value < 0 or value >= FAR_MINUTES else value


ENERGY_UNITS = {"kwh": 1.0, "wh": 0.001, "mwh": 1000.0}


def energy_kwh(value: float | None, unit: str | None) -> float | None:
    """kWh from an energy reading in kWh / Wh / MWh; None for other units (e.g. power)."""
    factor = ENERGY_UNITS.get((unit or "").strip().lower())
    return None if value is None or factor is None else value * factor


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

    By default (Deye) a program's time marks the START of its period, which runs until the next
    program's time; the last one runs until the first (P1 00:00, P2 03:00 = 00:00-03:00).
    time_is_end: the time marks the END of the period, which starts at the previous program's time.
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

    (minutes_state, duration_state, emergency_state), sensor_states, appliance_states, energy_states, program_states, weather = await asyncio.gather(
        asyncio.gather(*(ha.state(e) for e in (opts.outage_minutes_sensor, opts.outage_duration_sensor, opts.emergency_outage_sensor))),
        asyncio.gather(*(ha.state(entity) for entity in sensors.values())),
        asyncio.gather(*(ha.state(a.entity) for a in opts.appliances)),
        asyncio.gather(*(ha.state(a.energy_entity) for a in opts.appliances)),
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
    # Solar forecast correction (Settings): keep the sensor's own values for display.
    snapshot["forecast_raw"] = {k: snapshot.get(k) for k in ("today_forecast", "tomorrow_forecast")}
    for key in ("today_forecast", "tomorrow_forecast"):
        snapshot[key] = opts.adjust_forecast(snapshot.get(key))
    snapshot["appliances"] = {}
    # kWh counters for the monthly bill: the energy sensor, or a "power" sensor that is in fact
    # an energy counter (kWh / Wh).
    snapshot["appliance_energy"] = {}
    for appliance, state, energy_state in zip(opts.appliances, appliance_states, energy_states):
        if appliance.entity:
            unit = (state.get("attributes") or {}).get("unit_of_measurement") if state else None
            number = to_float(state.get("state")) if state else None
            if energy_kwh(number, unit) is not None:
                snapshot["appliance_energy"][appliance.id] = energy_kwh(number, unit)
            else:
                value = to_watts(number, unit)
                snapshot["appliances"][appliance.id] = value
                if value is None:
                    missing.append(appliance.entity)
        if appliance.energy_entity:
            unit = (energy_state.get("attributes") or {}).get("unit_of_measurement") if energy_state else None
            kwh = energy_kwh(to_float(energy_state.get("state")) if energy_state else None, unit or "kWh")
            if kwh is not None:
                snapshot["appliance_energy"][appliance.id] = kwh
            else:
                missing.append(appliance.energy_entity)
    snapshot["outdoor_temp"] = weather.get("outdoor_temp")
    snapshot["units"]["outdoor_temp"] = weather.get("unit")
    snapshot["weather"] = weather

    # Probable outages are derived from the outage sensors: "on" while a scheduled outage is
    # known (Minutes to outage below 9999), "off" otherwise; emergency outages count too.
    minutes = outage_minutes(minutes_state)
    emergency = is_on(emergency_state) if opts.emergency_outage_sensor else False
    if opts.outage_minutes_sensor or opts.emergency_outage_sensor:
        from outage_plan import duration_minutes  # outage_plan imports this module

        duration = duration_minutes(duration_state)
        outage_at = now + timedelta(minutes=minutes) if minutes is not None else None
        snapshot["outages_state"] = "on" if (minutes is not None or emergency) else "off"
        snapshot["outages_attrs"] = {
            "scheduled_outage": minutes is not None,
            "minutes_to_outage": round(minutes) if minutes is not None else None,
            "outage_starts": outage_at.isoformat(timespec="minutes") if outage_at else None,
            "outage_duration_minutes": round(duration) if duration is not None else None,
            "emergency_outages": emergency,
        }
    else:
        snapshot["outages_state"], snapshot["outages_attrs"] = None, {}

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
