"""Add-on options (written by the Supervisor to /data/options.json)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

OPTIONS_PATH = os.environ.get("BATTERYAI_OPTIONS", "/data/options.json")
DATA_DIR = os.environ.get("BATTERYAI_DATA", "/data")

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass
class DeyeProgram:
    slot: int
    time_entity: str
    soc_entity: str


@dataclass
class Options:
    claude_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "high"
    response_language: str = "English"
    analyses_per_day: int = 2
    first_analysis_time: str = "06:00"
    record_interval_minutes: int = 5
    history_days: int = 14
    today_forecast_sensor: str = ""
    tomorrow_forecast_sensor: str = ""
    battery_soc_sensor: str = ""
    outages_sensor: str = ""
    today_load_sensor: str = ""
    today_consumption_sensor: str = ""
    weekend_days: list[str] = field(default_factory=lambda: ["saturday", "sunday"])
    deye_programs: list[DeyeProgram] = field(default_factory=list)
    extra_instructions: str = ""

    def sensor_map(self) -> dict[str, str]:
        """Snapshot field name -> configured entity id."""
        return {
            "today_forecast": self.today_forecast_sensor,
            "tomorrow_forecast": self.tomorrow_forecast_sensor,
            "battery_soc": self.battery_soc_sensor,
            "outages": self.outages_sensor,
            "today_load": self.today_load_sensor,
            "today_consumption": self.today_consumption_sensor,
        }

    def analysis_times(self) -> list[tuple[int, int]]:
        """Evenly spread analysis times over the day, starting at first_analysis_time."""
        hour, minute = (int(part) for part in self.first_analysis_time.split(":"))
        start = hour * 60 + minute
        count = max(1, self.analyses_per_day)
        step = 1440 / count
        minutes = sorted({(start + round(i * step)) % 1440 for i in range(count)})
        return [(m // 60, m % 60) for m in minutes]

    def next_analysis(self, now: datetime, tz: tzinfo) -> datetime:
        local = now.astimezone(tz)
        candidates = []
        for day_offset in (0, 1):
            day = (local + timedelta(days=day_offset)).date()
            for hour, minute in self.analysis_times():
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                if candidate > local:
                    candidates.append(candidate)
        return min(candidates)


def load_options(path: str = OPTIONS_PATH) -> Options:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}

    known = {name for name in Options.__dataclass_fields__ if name != "deye_programs"}
    opts = Options(**{key: value for key, value in raw.items() if key in known and value is not None})
    opts.weekend_days = [day.lower() for day in opts.weekend_days]
    opts.deye_programs = [
        DeyeProgram(
            slot=index + 1,
            time_entity=(program.get("time_entity") or "").strip(),
            soc_entity=(program.get("soc_entity") or "").strip(),
        )
        for index, program in enumerate(raw.get("deye_programs") or [])
    ]
    return opts
