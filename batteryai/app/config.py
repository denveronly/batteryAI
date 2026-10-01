"""Settings, edited in the panel's Settings tab and stored in /data/settings.json."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, tzinfo
from typing import Any

OPTIONS_PATH = os.environ.get("BATTERYAI_OPTIONS", "/data/options.json")
DATA_DIR = os.environ.get("BATTERYAI_DATA", "/data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEYE_PROGRAM_COUNT = 6
SENSOR_KEYS = (
    "today_forecast_sensor",
    "tomorrow_forecast_sensor",
    "battery_soc_sensor",
    "outages_sensor",
    "today_load_sensor",
    "today_consumption_sensor",
)
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass
class DeyeProgram:
    slot: int
    time_entity: str = ""
    soc_entity: str = ""


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """Settings for the UI; the API key itself never leaves the add-on."""
        data = self.to_dict()
        data["claude_api_key_set"] = bool(data.pop("claude_api_key"))
        return data


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {msg}" for key, msg in errors.items()))
        self.errors = errors


def parse_settings(raw: dict[str, Any], current: Options | None = None) -> Options:
    """Validates settings from the UI (or the legacy add-on options)."""
    errors: dict[str, str] = {}
    base = current or Options()

    def text(key: str) -> str:
        value = raw.get(key, getattr(base, key))
        return "" if value is None else str(value).strip()

    def integer(key: str, low: int, high: int) -> int:
        value = raw.get(key, getattr(base, key))
        try:
            number = int(value)
        except (TypeError, ValueError):
            errors[key] = "must be a whole number"
            return getattr(base, key)
        if not low <= number <= high:
            errors[key] = f"must be between {low} and {high}"
        return number

    def entity(key: str, value: str) -> str:
        if value and not ENTITY_RE.match(value):
            errors[key] = "must look like sensor.my_sensor (lowercase domain.object_id)"
        return value

    api_key = text("claude_api_key") if raw.get("claude_api_key") else base.claude_api_key
    opts = Options(
        claude_api_key=api_key,
        claude_model=text("claude_model") or "claude-opus-5-5",
        claude_effort=text("claude_effort"),
        response_language=text("response_language") or "English",
        analyses_per_day=integer("analyses_per_day", 1, 24),
        first_analysis_time=text("first_analysis_time"),
        record_interval_minutes=integer("record_interval_minutes", 1, 60),
        history_days=integer("history_days", 1, 90),
        extra_instructions=text("extra_instructions"),
    )
    for key in SENSOR_KEYS:
        setattr(opts, key, entity(key, text(key)))

    if opts.claude_effort not in EFFORTS:
        errors["claude_effort"] = "must be one of " + ", ".join(EFFORTS)
    if not TIME_RE.match(opts.first_analysis_time):
        errors["first_analysis_time"] = "must be HH:MM (24-hour)"

    weekend = raw.get("weekend_days", base.weekend_days) or []
    opts.weekend_days = [str(day).lower() for day in weekend if str(day).lower() in WEEKDAYS]

    programs = raw.get("deye_programs")
    if programs is None:
        programs = [asdict(p) for p in base.deye_programs]
    opts.deye_programs = []
    for index in range(DEYE_PROGRAM_COUNT):
        item = programs[index] if index < len(programs) and isinstance(programs[index], dict) else {}
        slot = index + 1
        time_entity = entity(f"deye_programs.{slot}.time_entity", str(item.get("time_entity") or "").strip())
        soc_entity = entity(f"deye_programs.{slot}.soc_entity", str(item.get("soc_entity") or "").strip())
        opts.deye_programs.append(DeyeProgram(slot=slot, time_entity=time_entity, soc_entity=soc_entity))

    if errors:
        raise SettingsError(errors)
    return opts


def load_settings() -> Options:
    """settings.json if saved from the UI; otherwise import the old add-on options once."""
    for path in (SETTINGS_PATH, OPTIONS_PATH):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if not raw:
            continue
        try:
            return parse_settings(raw)
        except SettingsError:
            # Keep whatever is valid so the UI can show and fix the rest.
            return _lenient(raw)
    return parse_settings({})


def _lenient(raw: dict[str, Any]) -> Options:
    opts = Options()
    for item in fields(Options):
        if item.name in raw and item.name != "deye_programs":
            try:
                opts = parse_settings({item.name: raw[item.name]}, opts)
            except SettingsError:
                pass
    try:
        opts = parse_settings({"deye_programs": raw.get("deye_programs") or []}, opts)
    except SettingsError:
        opts = parse_settings({}, opts)
    return opts


def save_settings(opts: Options) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(opts.to_dict(), fh, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_PATH)
