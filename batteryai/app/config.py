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
class Appliance:
    """A device with its own power sensor (W), e.g. a heat pump, boiler or EV charger."""

    id: str  # stable key used in the database; kept when the appliance is renamed
    name: str
    entity: str = ""
    temperature_dependent: bool = False  # heats or cools: energy follows outdoor temperature


@dataclass
class Tariff:
    """A grid price that applies in its time windows; the default one applies at all other times."""

    name: str
    price: float
    windows: list[str] = field(default_factory=list)  # "HH:MM-HH:MM", may cross midnight
    default: bool = False


MAX_TARIFFS = 8
# 2: program ranges default to "start" (0.4.8). Up to 0.4.7 "end" was the default and was
# saved into every settings.json, so it is switched to "start" once on load.
SETTINGS_VERSION = 2


def _window_minutes(window: str) -> tuple[int, int]:
    start, end = window.split("-")
    return int(start[:2]) * 60 + int(start[3:5]), int(end[:2]) * 60 + int(end[3:5])


def _in_window(minute: int, start: int, end: int) -> bool:
    return start <= minute < end if start < end else minute >= start or minute < end


MAX_APPLIANCES = 12
# Up to 0.4.0 there were three fixed appliance settings; they become the first entries.
LEGACY_APPLIANCES = (
    ("heat_pump", "Heat pump", "heat_pump_power_sensor", True),
    ("boiler", "Boiler", "boiler_power_sensor", False),
    ("ev", "EV charger", "ev_power_sensor", False),
)


def appliance_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "appliance"
    if base[0].isdigit():
        base = "a_" + base
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


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
    appliances: list[Appliance] = field(default_factory=list)
    pv_energy_sensor: str = ""
    pv_power_sensor: str = ""
    grid_import_sensor: str = ""
    tariff_currency: str = "UAH"
    tariffs: list[Tariff] = field(default_factory=lambda: [
        Tariff(name="Off-peak", price=2.16, windows=["23:00-07:00"]),
        Tariff(name="Peak", price=4.32, default=True),
    ])
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
    # "start" (Deye default): a program runs from its own time until the next program's
    # time (P6 runs until P1). "end": a program's time is the END of its period, which
    # starts at the previous program's time.
    program_time_marks: str = "start"
    settings_version: int = SETTINGS_VERSION
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

    def tariff_for(self, minute_of_day: int) -> Tariff | None:
        """The tariff in effect: the first one whose window contains the minute, else the default."""
        for tariff in self.tariffs:
            if not tariff.default and any(_in_window(minute_of_day, *_window_minutes(w)) for w in tariff.windows):
                return tariff
        return next((t for t in self.tariffs if t.default), None)

    def tariff_at(self, minute_of_day: int) -> tuple[float, str]:
        """(price per kWh, tariff name) at a minute of the day."""
        tariff = self.tariff_for(minute_of_day)
        return (tariff.price, tariff.name) if tariff else (0.0, "")

    @property
    def cheapest_price(self) -> float:
        return min((t.price for t in self.tariffs), default=0.0)

    @property
    def single_price(self) -> bool:
        """One price at all times (one tariff, or all at the same price): no point in shifting."""
        return len({t.price for t in self.tariffs}) <= 1

    def is_cheap(self, minute_of_day: int) -> bool:
        """True in the cheapest tariff's hours: the best time to charge from the grid."""
        return self.tariff_at(minute_of_day)[0] <= self.cheapest_price + 1e-9

    def tariff_dict(self) -> dict[str, Any]:
        return {
            "currency": self.tariff_currency,
            "tariffs": [
                {"name": t.name, "price_per_kwh": t.price, "windows": ("all day" if len(self.tariffs) == 1 else "all other times") if t.default else t.windows}
                for t in self.tariffs
            ],
            "cheapest": next((t.name for t in self.tariffs if t.price == self.cheapest_price), None),
            "single_price": self.single_price,
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

    opts.program_time_marks = text("program_time_marks") or "start"
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
    items = raw.get("tariffs")
    if items is None and any(k in raw for k in ("tariff_peak_price", "tariff_offpeak_price", "tariff_offpeak_windows")):
        # Up to 0.4.5 there was one peak and one off-peak price.
        windows = raw.get("tariff_offpeak_windows") or ["23:00-07:00"]
        items = [
            {"name": "Off-peak", "price": raw.get("tariff_offpeak_price", 2.16), "windows": windows},
            {"name": "Peak", "price": raw.get("tariff_peak_price", 4.32), "windows": [], "default": True},
        ]
    if items is None:
        items = [asdict(t) for t in base.tariffs]
    opts.tariffs = []
    names: set[str] = set()

    def _windows_of(item: dict[str, Any]) -> list[Any]:
        raw_windows = item.get("windows") or []
        return raw_windows.replace(";", ",").split(",") if isinstance(raw_windows, str) else list(raw_windows)

    def _is_empty(item: dict[str, Any]) -> bool:  # a row added with + and never filled in
        return (
            not str(item.get("name") or "").strip()
            and not str(item.get("price") or "").strip()
            and not any(str(w).strip() for w in _windows_of(item))
        )

    rows = [item for item in (items[:MAX_TARIFFS] if isinstance(items, list) else []) if isinstance(item, dict)]
    # A single tariff is one price all day, whatever its windows say.
    single = sum(not _is_empty(item) for item in rows) == 1
    for index, item in enumerate(rows):
        if _is_empty(item):
            continue
        name = str(item.get("name") or "").strip()[:30]
        raw_windows = _windows_of(item)
        if not name:
            errors[f"tariffs.{index}.name"] = "give the tariff a name"
        elif name.lower() in names:
            errors[f"tariffs.{index}.name"] = "two tariffs have the same name"
        names.add(name.lower())
        try:
            tariff_price = float(str(item.get("price")).replace(",", "."))
            if tariff_price < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors[f"tariffs.{index}.price"] = "enter a price per kWh"
            tariff_price = 0.0
        windows = []
        for window in (str(w).replace(" ", "").replace("–", "-") for w in raw_windows):
            if not window:
                continue
            parts = window.split("-")
            if len(parts) != 2 or not all(TIME_RE.match(p.zfill(5)) for p in parts):
                errors[f"tariffs.{index}.windows"] = f"'{window}' must look like 23:00-07:00"
                continue
            windows.append("-".join(p.zfill(5) for p in parts))
        is_default = bool(item.get("default")) or single
        if single:
            errors.pop(f"tariffs.{index}.windows", None)
        if not is_default and not windows and f"tariffs.{index}.windows" not in errors:
            errors[f"tariffs.{index}.windows"] = "add a time window, or mark it as “all other times”"
        opts.tariffs.append(Tariff(name=name, price=tariff_price, windows=[] if is_default else windows, default=is_default))
    defaults = [t for t in opts.tariffs if t.default]
    if not opts.tariffs:
        errors["tariffs"] = "add at least one tariff"
    elif len(opts.tariffs) == 1 and not defaults:
        opts.tariffs[0].default, opts.tariffs[0].windows = True, []
    elif len(defaults) != 1:
        errors["tariffs"] = "mark exactly one tariff as “all other times”"

    items = raw.get("appliances")
    if items is None:
        items = [asdict(a) for a in base.appliances]
    if not items and any(raw.get(key) for _, _, key, _ in LEGACY_APPLIANCES):
        items = [
            {"id": app_id, "name": name, "entity": raw[key], "temperature_dependent": temp}
            for app_id, name, key, temp in LEGACY_APPLIANCES
            if raw.get(key)
        ]
    opts.appliances = []
    taken: set[str] = set()
    for index, item in enumerate(items[:MAX_APPLIANCES] if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        entity_id = entity(f"appliances.{index}.entity", str(item.get("entity") or "").strip())
        if not name and not entity_id:
            continue  # an empty row added with + and never filled in
        if not name:
            errors[f"appliances.{index}.name"] = "give the appliance a name"
        app_id = str(item.get("id") or "").strip()
        if not re.match(r"^[a-z][a-z0-9_]{0,40}$", app_id) or app_id in taken:
            app_id = appliance_id(name, taken)
        taken.add(app_id)
        opts.appliances.append(Appliance(
            id=app_id, name=name, entity=entity_id, temperature_dependent=bool(item.get("temperature_dependent")),
        ))

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
        if int(raw.get("settings_version") or 1) < 2 and raw.get("program_time_marks") == "end":
            raw["program_time_marks"] = "start"
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
    legacy = {key: raw[key] for _, _, key, _ in LEGACY_APPLIANCES if key in raw and "appliances" not in raw}
    legacy.update({
        key: raw[key]
        for key in ("tariff_peak_price", "tariff_offpeak_price", "tariff_offpeak_windows")
        if key in raw and "tariffs" not in raw
    })
    if legacy:
        try:
            opts = parse_settings(legacy, opts)
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
