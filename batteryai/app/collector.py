"""Reads the configured Home Assistant entities into one snapshot."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, tzinfo
from typing import Any

from config import WEEKDAYS, Options
from ha import HomeAssistant

UNAVAILABLE = {"", "unknown", "unavailable", "none", "null"}
MAX_ATTRIBUTES_CHARS = 4000
NUMERIC_FIELDS = ("battery_soc", "today_forecast", "tomorrow_forecast", "today_load", "today_consumption")


def to_float(value: Any) -> float | None:
    if value is None or str(value).strip().lower() in UNAVAILABLE:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def active_program(programs: list[dict[str, Any]], minute_of_day: int) -> dict[str, Any] | None:
    """The Deye program in effect: the latest start time at or before now, wrapping past midnight."""
    timed = [(parse_hhmm(p.get("time")), p) for p in programs]
    timed = sorted(((start, p) for start, p in timed if start is not None), key=lambda item: item[0])
    if not timed:
        return None
    current = timed[-1][1]
    for start, program in timed:
        if start <= minute_of_day:
            current = program
    return current


def _trim_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    text = json.dumps(attributes, default=str, ensure_ascii=False)
    if len(text) <= MAX_ATTRIBUTES_CHARS:
        return attributes
    return {"truncated_json": text[:MAX_ATTRIBUTES_CHARS]}


async def collect(ha: HomeAssistant, opts: Options, tz: tzinfo) -> dict[str, Any]:
    now = datetime.now(tz)
    sensors = opts.sensor_map()
    program_entities = [(p.time_entity, p.soc_entity) for p in opts.deye_programs]

    sensor_states, program_states = await asyncio.gather(
        asyncio.gather(*(ha.state(entity) for entity in sensors.values())),
        asyncio.gather(*(asyncio.gather(ha.state(t), ha.state(s)) for t, s in program_entities)),
    )
    by_field = dict(zip(sensors.keys(), sensor_states))

    missing = [
        entity
        for name, entity in sensors.items()
        if entity and (by_field[name] is None or clean_state(by_field[name].get("state")) is None)
    ]

    snapshot: dict[str, Any] = {
        "ts": int(now.timestamp()),
        "local_time": now.isoformat(timespec="minutes"),
        "local_date": now.date().isoformat(),
        "weekday": WEEKDAYS[now.weekday()],
        "is_weekend": WEEKDAYS[now.weekday()] in opts.weekend_days,
        "units": {},
    }
    for name in NUMERIC_FIELDS:
        state = by_field.get(name)
        snapshot[name] = to_float(state.get("state")) if state else None
        if state:
            snapshot["units"][name] = state.get("attributes", {}).get("unit_of_measurement")

    outages = by_field.get("outages")
    snapshot["outages_state"] = clean_state(outages.get("state")) if outages else None
    snapshot["outages_attrs"] = _trim_attributes(outages.get("attributes")) if outages else {}

    programs = []
    for program, (time_state, soc_state) in zip(opts.deye_programs, program_states):
        time_value = clean_state(time_state.get("state")) if time_state else None
        soc_value = to_float(soc_state.get("state")) if soc_state else None
        if program.time_entity and time_value is None:
            missing.append(program.time_entity)
        if program.soc_entity and soc_value is None:
            missing.append(program.soc_entity)
        programs.append({"slot": program.slot, "time": time_value, "soc": soc_value})
    snapshot["deye_programs"] = programs

    current = active_program(programs, now.hour * 60 + now.minute)
    snapshot["active_program_slot"] = current["slot"] if current else None
    snapshot["missing_entities"] = missing
    return snapshot


def has_data(snapshot: dict[str, Any]) -> bool:
    return any(snapshot.get(name) is not None for name in NUMERIC_FIELDS) or any(
        p["time"] is not None or p["soc"] is not None for p in snapshot.get("deye_programs", [])
    )
