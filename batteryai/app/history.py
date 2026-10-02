"""Imports past values of the configured entities from the Home Assistant recorder."""

from __future__ import annotations

import logging
from bisect import bisect_right
from datetime import datetime, timedelta, tzinfo
from typing import Any

from collector import POWER_FIELDS, active_program, clean_state, to_float, to_watts
from config import WEEKDAYS, Options
from db import Database
from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)

CHUNK = timedelta(days=2)  # keeps each history request small


def _parse_ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


class Series:
    """State changes of one entity; value_at(ts) returns the value in effect at ts."""

    def __init__(self, points: list[tuple[float, Any]]) -> None:
        points.sort(key=lambda p: p[0])
        self.times = [p[0] for p in points]
        self.values = [p[1] for p in points]

    def value_at(self, ts: float) -> Any:
        index = bisect_right(self.times, ts) - 1
        return self.values[index] if index >= 0 else None


async def _fetch(
    ha: HomeAssistant, entity_id: str, start: datetime, end: datetime, attribute: str | None = None
) -> list[tuple[float, Any]]:
    points: list[tuple[float, Any]] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(end, chunk_start + CHUNK)
        states = await ha.history(entity_id, chunk_start, chunk_end, with_attributes=attribute is not None)
        for item in states:
            ts = _parse_ts(item.get("last_changed") or item.get("last_updated"))
            if ts is None:
                continue
            value = (item.get("attributes") or {}).get(attribute) if attribute else item.get("state")
            points.append((ts, value))
        chunk_start = chunk_end
    return points


async def import_history(
    ha: HomeAssistant, opts: Options, db: Database, tz: tzinfo, days: int, progress: dict[str, Any]
) -> dict[str, Any]:
    """Rebuilds readings for the last `days` days from the HA recorder, at the recording interval."""
    end = datetime.now(tz).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    sensors = {name: entity for name, entity in opts.sensor_map().items() if entity}
    entities: dict[str, tuple[str, str | None]] = {name: (entity, None) for name, entity in sensors.items()}
    if opts.weather_entity:
        attribute = "temperature" if opts.weather_entity.startswith("weather.") else None
        entities["outdoor_temp"] = (opts.weather_entity, attribute)
    appliance_keys = {f"appliance:{a.id}": a.id for a in opts.appliances if a.entity}
    for appliance in opts.appliances:
        if appliance.entity:
            entities[f"appliance:{appliance.id}"] = (appliance.entity, None)
    for program in opts.deye_programs:
        if program.time_entity:
            entities[f"program_{program.slot}_time"] = (program.time_entity, None)
        if program.soc_entity:
            entities[f"program_{program.slot}_soc"] = (program.soc_entity, None)

    units: dict[str, str | None] = {}
    series: dict[str, Series] = {}
    failed: dict[str, str] = {}
    progress.update(total=len(entities), done=0)
    for name, (entity_id, attribute) in entities.items():
        progress["current"] = entity_id
        try:
            if name in POWER_FIELDS or name in appliance_keys:
                units[name] = ((await ha.fetch_state(entity_id)).get("attributes") or {}).get("unit_of_measurement")
            series[name] = Series(await _fetch(ha, entity_id, start, end, attribute))
        except HAError as err:
            failed[entity_id] = str(err)
            _LOGGER.warning("History import of %s failed: %s", entity_id, err)
        progress["done"] += 1

    def value(name: str, ts: float) -> Any:
        return series[name].value_at(ts) if name in series else None

    interval = opts.record_interval_minutes * 60
    first = int(start.timestamp()) // interval * interval + interval
    snapshots = []
    for ts in range(first, int(end.timestamp()), interval):
        local = datetime.fromtimestamp(ts, tz)
        snap: dict[str, Any] = {
            "ts": ts,
            "weekday": WEEKDAYS[local.weekday()],
            "is_weekend": WEEKDAYS[local.weekday()] in opts.weekend_days,
            "outages_state": clean_state(value("outages", ts)),
            "outages_attrs": {},
            "outdoor_temp": to_float(value("outdoor_temp", ts)),
        }
        for name in sensors:
            if name == "outages":
                continue
            number = to_float(value(name, ts))
            snap[name] = to_watts(number, units.get(name)) if name in POWER_FIELDS else number
        snap["appliances"] = {
            app_id: to_watts(to_float(value(key, ts)), units.get(key)) for key, app_id in appliance_keys.items()
        }
        programs = [
            {
                "slot": p.slot,
                "time": clean_state(value(f"program_{p.slot}_time", ts)),
                "soc": to_float(value(f"program_{p.slot}_soc", ts)),
            }
            for p in opts.deye_programs
        ]
        snap["deye_programs"] = programs
        current = active_program(programs, local.hour * 60 + local.minute, opts.program_time_marks == "end")
        snap["active_program_slot"] = current["slot"] if current else None
        if any(snap.get(name) is not None for name in sensors) or snap["outdoor_temp"] is not None:
            snapshots.append(snap)

    db.replace_range(int(start.timestamp()), int(end.timestamp()), snapshots, tz)
    result = {
        "from": start.isoformat(timespec="minutes"),
        "to": end.isoformat(timespec="minutes"),
        "readings": len(snapshots),
        "entities": len(series),
        "failed": failed,
    }
    _LOGGER.info("History import finished: %s", result)
    return result
