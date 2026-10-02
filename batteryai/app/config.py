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
ENGINES = ("claude", "local_fast", "local_llm")
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEYE_PROGRAM_COUNT = 6
SENSOR_KEYS = (
    "today_forecast_sensor",
    "tomorrow_forecast_sensor",
    "battery_soc_sensor",
    "outages_sensor",
    "load_power_sensor",
    "today_consumption_sensor",
    "weather_entity",
    "heat_pump_power_sensor",
    "boiler_power_sensor",
    "ev_power_sensor",
    "pv_energy_sensor",
    "pv_power_sensor",
    "grid_import_sensor",
)
# Settings saved by 0.1/0.2 used "today_load_sensor" for what is a load power sensor.
LEGACY_KEYS = {"load_power_sensor": "today_load_sensor"}
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass
class DeyeProgram:
    slot: int
    time_entity: str = ""
    soc_entity: str = ""
    charge_entity: str = ""  # optional grid-charge switch of the program


@dataclass
class Options:
    claude_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "high"
    response_language: str = "English"
    # Prediction runs (HH:MM, Home Assistant time zone); with auto-control on, each run
    # pushes new SOC values to the inverter.
    analysis_times_list: list[str] = field(default_factory=lambda: ["12:00", "23:00"])
    record_interval_minutes: int = 5
    history_days: int = 14
    # Readings older than this are compressed to hourly rows to keep the database small.
    detail_days: int = 30
    today_forecast_sensor: str = ""
    tomorrow_forecast_sensor: str = ""
    battery_soc_sensor: str = ""
    outages_sensor: str = ""
    load_power_sensor: str = ""
    today_consumption_sensor: str = ""
    weather_entity: str = ""
    heat_pump_power_sensor: str = ""
    boiler_power_sensor: str = ""
    ev_power_sensor: str = ""
    pv_energy_sensor: str = ""
    pv_power_sensor: str = ""
    grid_import_sensor: str = ""
    tariff_currency: str = "UAH"
    tariff_peak_price: float = 4.32
    tariff_offpeak_price: float = 2.16
    tariff_offpeak_windows: list[str] = field(default_factory=lambda: ["23:00-07:00"])
    prediction_margin_percent: int = 10
    min_soc_percent: int = 20
    max_soc_percent: int = 100
    apply_threshold_percent: int = 5
    charge_all_soc_percent: int = 98
    battery_capacity_kwh: float = 10.0
    # "claude" | "local_fast" (statistics + rules) | "local_llm" (Qwen2.5 3B in the add-on)
    prediction_engine: str = "claude"
    local_llm_threads: int = 0  # 0 = all CPU cores
    weekend_days: list[str] = field(default_factory=lambda: ["saturday", "sunday"])
    deye_programs: list[DeyeProgram] = field(default_factory=list)
    # "end": a program's time is the END of its period, which starts at the previous
    # program's time. "start": the time starts the period, which lasts until the next one.
    program_time_marks: str = "end"
    extra_instructions: str = ""
    notify_services: list[str] = field(default_factory=list)
    notify_predictions: bool = True
    notify_soc_changes: bool = True
    notify_errors: bool = True

    def sensor_map(self) -> dict[str, str]:
        """Snapshot field name -> configured entity id."""
        return {
            "today_forecast": self.today_forecast_sensor,
            "tomorrow_forecast": self.tomorrow_forecast_sensor,
            "battery_soc": self.battery_soc_sensor,
            "outages": self.outages_sensor,
            "load_power": self.load_power_sensor,
            "today_consumption": self.today_consumption_sensor,
            "heat_pump_power": self.heat_pump_power_sensor,
            "boiler_power": self.boiler_power_sensor,
            "ev_power": self.ev_power_sensor,
            "pv_today": self.pv_energy_sensor,
            "pv_power": self.pv_power_sensor,
            "grid_import_today": self.grid_import_sensor,
        }

    def analysis_times(self) -> list[tuple[int, int]]:
        minutes = sorted({int(t[:2]) * 60 + int(t[3:5]) for t in self.analysis_times_list})
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

    def offpeak_ranges(self) -> list[tuple[int, int]]:
        """Off-peak windows as (start, end) minutes; a window may wrap past midnight."""
        ranges = []
        for window in self.tariff_offpeak_windows:
            start, end = window.split("-")
            ranges.append((int(start[:2]) * 60 + int(start[3:5]), int(end[:2]) * 60 + int(end[3:5])))
        return ranges

    def tariff_at(self, minute_of_day: int) -> tuple[float, str]:
        """(price per kWh, "offpeak" | "peak") at a minute of the day."""
        for start, end in self.offpeak_ranges():
            inside = start <= minute_of_day < end if start < end else minute_of_day >= start or minute_of_day < end
            if inside:
                return self.tariff_offpeak_price, "offpeak"
        return self.tariff_peak_price, "peak"

    def tariff_dict(self) -> dict[str, Any]:
        return {
            "currency": self.tariff_currency,
            "peak_price_per_kwh": self.tariff_peak_price,
            "offpeak_price_per_kwh": self.tariff_offpeak_price,
            "offpeak_windows": self.tariff_offpeak_windows,
            "peak": "all other times",
        }

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
        legacy = LEGACY_KEYS.get(key)
        if key not in raw and legacy in raw:
            value = raw[legacy]
        else:
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
        record_interval_minutes=integer("record_interval_minutes", 1, 60),
        history_days=integer("history_days", 1, 365),
        detail_days=integer("detail_days", 2, 365),
        extra_instructions=text("extra_instructions"),
        prediction_margin_percent=integer("prediction_margin_percent", 0, 100),
        min_soc_percent=integer("min_soc_percent", 0, 100),
        max_soc_percent=integer("max_soc_percent", 0, 100),
        apply_threshold_percent=integer("apply_threshold_percent", 0, 50),
        charge_all_soc_percent=integer("charge_all_soc_percent", 10, 100),
    )
    if opts.min_soc_percent > opts.max_soc_percent:
        errors["min_soc_percent"] = "must not be higher than the maximum SOC"
    for key in SENSOR_KEYS:
        setattr(opts, key, entity(key, text(key)))

    opts.program_time_marks = text("program_time_marks") or "end"
    if opts.program_time_marks not in ("end", "start"):
        errors["program_time_marks"] = "must be end or start"
    if opts.claude_effort not in EFFORTS:
        errors["claude_effort"] = "must be one of " + ", ".join(EFFORTS)
    times = raw.get("analysis_times_list", base.analysis_times_list)
    if isinstance(times, str):
        times = times.replace(";", ",").split(",")
    times = [str(t).strip()[:5] for t in times or [] if str(t).strip()]
    times = [t if len(t) == 5 else t.zfill(5) for t in times]
    bad = [t for t in times if not TIME_RE.match(t)]
    if bad:
        errors["analysis_times_list"] = "times must be HH:MM (24-hour): " + ", ".join(bad)
    elif not 1 <= len(set(times)) <= 24:
        errors["analysis_times_list"] = "enter between 1 and 24 times"
    opts.analysis_times_list = sorted(set(times))

    def price(key: str) -> float:
        value = raw.get(key, getattr(base, key))
        try:
            number = float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            errors[key] = "must be a number"
            return getattr(base, key)
        if number < 0:
            errors[key] = "must not be negative"
        return number

    opts.battery_capacity_kwh = price("battery_capacity_kwh")
    if opts.battery_capacity_kwh <= 0:
        errors["battery_capacity_kwh"] = "must be more than 0"
    opts.prediction_engine = text("prediction_engine") or "claude"
    if opts.prediction_engine not in ENGINES:
        errors["prediction_engine"] = "must be one of " + ", ".join(ENGINES)
    opts.local_llm_threads = integer("local_llm_threads", 0, 64)
    opts.tariff_currency = text("tariff_currency")[:8]
    opts.tariff_peak_price = price("tariff_peak_price")
    opts.tariff_offpeak_price = price("tariff_offpeak_price")
    windows = raw.get("tariff_offpeak_windows", base.tariff_offpeak_windows)
    if isinstance(windows, str):
        windows = windows.replace(";", ",").split(",")
    opts.tariff_offpeak_windows = []
    for window in (str(w).replace(" ", "").replace("–", "-") for w in windows or []):
        if not window:
            continue
        parts = window.split("-")
        if len(parts) != 2 or not all(TIME_RE.match(p.zfill(5)) for p in parts):
            errors["tariff_offpeak_windows"] = f"'{window}' must look like 23:00-07:00"
            continue
        opts.tariff_offpeak_windows.append("-".join(p.zfill(5) for p in parts))

    services = raw.get("notify_services", base.notify_services) or []
    if isinstance(services, str):
        services = services.split(",")
    opts.notify_services = []
    for service in (str(x).strip() for x in services):
        if not service:
            continue
        service = service.removeprefix("notify.")
        if not re.match(r"^[a-z0-9_]+$", service):
            errors["notify_services"] = f"'{service}' is not a notify service name"
        opts.notify_services.append(service)
    for key in ("notify_predictions", "notify_soc_changes", "notify_errors"):
        value = raw.get(key, getattr(base, key))
        setattr(opts, key, value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes"))

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
        charge_entity = entity(
            f"deye_programs.{slot}.charge_entity", str(item.get("charge_entity") or "").strip()
        )
        opts.deye_programs.append(
            DeyeProgram(slot=slot, time_entity=time_entity, soc_entity=soc_entity, charge_entity=charge_entity)
        )

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
        for key, legacy in LEGACY_KEYS.items():
            if key not in raw and legacy in raw:
                raw[key] = raw[legacy]
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
