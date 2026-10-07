"""Builds the analysis input from recorded data and asks Claude for a plan."""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import anthropic

from config import Options
from db import Database, accuracy_report, all_days, monthly_summary

_LOGGER = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
RECENT_HOURS = 48
# Adaptive thinking counts toward max_tokens; stream so a long answer can't hit the HTTP timeout.
MAX_TOKENS = 64000

SYSTEM_PROMPT = """You are BatteryAI, an energy analyst for a home in Home Assistant with solar panels, a battery and a Deye hybrid inverter. The owner lists the household's big appliances (for example a heat pump, an electric boiler or an EV charger), each with its own power sensor; appliances marked temperature_dependent heat or cool the house, so their use follows the outdoor temperature.

Each request gives you one JSON document with:
- current: the latest values: battery SOC, PV power and load power right now (pv_surplus_w > 0 means the battery is being charged by the sun), solar forecast for today and tomorrow, total load power (W), today's consumption counter (kWh), the power of each listed appliance (W), outdoor temperature, probable outages (outages_state on/off, with minutes_to_outage, outage_starts, outage_duration_minutes and emergency_outages in its attributes), local time and weekday.
- plan_day: the day this plan is for (date, label today/tomorrow, weekday, solar forecast, weather).
- recent_outage_days: days of the last week with a scheduled or emergency outage.
- weather: the current condition and the forecast for today and tomorrow (daily and, when available, hourly temperatures).
- deye_programs: the inverter's six time-of-use programs. Each has a range (already worked out for you, e.g. "23:15-05:00", which crosses midnight) during which the inverter keeps the battery at or above the program's SOC capacity. "time" is only the value of the program's time setting; always reason with "range". When a program has grid_charge ("on"/"off"), that is its grid-charge switch: when on, the inverter charges the battery from the grid up to the program's SOC.
- schedule: when this plan is applied and when the next run will replace it. Plan for the whole period until the next run.
- appliances: the listed appliances (id, name, temperature_dependent).
- daily_history: one row per day with consumption, PV production, grid import, the solar forecast, energy used by each appliance (by id) and the hours it was running, outdoor temperatures, min/max SOC, weekday and weekend flag.
- monthly_history: one row per calendar month over all recorded history (consumption, PV, grid import, appliances, average temperature, weekday/weekend averages). PV and usage change a lot with the season: use it to judge what is normal for this time of year.
- same_period_last_year: daily rows from around this date last year, when recorded.
- hourly_profile: average power per hour of day for the load, PV and each appliance, split into weekdays and weekends, with the average outdoor temperature for that hour.
- recent_hourly: hourly samples from the last 48 hours.
- accuracy: your earlier predictions compared with what actually happened, as percentages.
- tuning: the owner's settings for planning (safety margin, allowed SOC range).
- tariff: the grid tariffs (name, price per kWh, time windows; one applies at all other times), the currency, the cheapest tariff and the one in effect now.
- user_notes: optional instructions from the owner.

Weekends usually use less energy than weekdays in this home. For temperature-dependent appliances, relate their energy to the outdoor temperature in the history and use the plan day's forecast temperatures to predict it. Check every assumption against the data instead of assuming it.

Your tasks:
1. Predict consumption for the rest of today and for the plan day (plan_day: "today" when the prediction runs in the morning, before 13:00, because today's programs are still ahead; otherwise "tomorrow"). Every result field named ..._tomorrow refers to the plan day, and the summary must speak of the plan day (say today or tomorrow as plan_day.label says). Predict when each listed appliance will run and how much energy it will use (refer to appliances by their id). Use weekday/weekend patterns, temperature, the solar forecast and your past accuracy (correct systematic over- or under-prediction).
2. Give an hourly forecast for the plan day (average W per hour for total load and each appliance).
3. Judge the outage risk from the probable outages (scheduled outage start and duration, emergency outages) and make sure the battery will hold enough charge to cover the expected outage windows. recent_outage_days lists the days of the last week with outages: if there are any, be stricter and keep the battery full for the evening and night, when there is no PV.
4. Propose an SOC capacity for each Deye program. Plan for the predicted consumption increased by tuning.prediction_margin_percent, keep every SOC between tuning.min_soc_percent and tuning.max_soc_percent, balance outage backup, solar self-consumption and grid charging, and explain every change. Program times are fixed by the owner and are never changed by BatteryAI: return each program's current time unchanged and do not suggest moving times.
5. Decide grid charge (grid_charge) for each program that has a switch. Turn it on and raise the SOC before an expected outage when the battery would otherwise not cover the load until power returns, taking into account the time of day: if the outage falls in daylight hours and the PV forecast covers the load and recharges the battery, grid charging is not needed. Turn it off when PV is expected to be enough, so the battery is charged by the sun. For programs without a switch return null.
6. Minimise what is paid for grid energy: charge from the grid in the cheapest tariff windows, use PV first, and cover the load in the more expensive tariff periods from the battery. With several tariffs, a night program in a pricier tariff gets grid_charge only if the battery would not last until the cheapest tariff begins, given the expected consumption and PV. If tariff.single_price is true, grid energy costs the same at all times: charging from the grid saves nothing in daytime programs, so keep grid_charge off there and let PV charge the battery. In the evening and night programs (no PV) always keep grid_charge on, so the battery can recharge for an unplanned emergency outage. Estimate the plan day's grid cost in the tariff currency.
7. Give short, practical recommendations.

Use the units the sensors report (W for power, kWh for energy, % for SOC). If data is missing, stale or implausible, say so in the summary and lower your confidence; never invent values. """


RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "predicted_consumption_rest_of_today_kwh": {"type": "number"},
        "predicted_consumption_tomorrow_kwh": {"type": "number"},
        "predicted_pv_tomorrow_kwh": {"type": "number"},
        "predicted_min_soc_percent": {"type": "number"},
        "outage_risk": {"type": "string", "enum": ["none", "low", "medium", "high", "unknown"]},
        "weather_impact": {"type": "string"},
        "estimated_grid_cost_tomorrow": {"type": "number"},
        "appliance_forecast": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "appliance": {"type": "string"},
                    "expected_kwh_tomorrow": {"type": "number"},
                    "expected_usage_windows": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["appliance", "expected_kwh_tomorrow", "expected_usage_windows", "reason"],
                "additionalProperties": False,
            },
        },
        "hourly_forecast_tomorrow": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hour": {"type": "integer"},
                    "load_w": {"type": "number"},
                },
                "required": ["hour", "load_w"],
                "additionalProperties": False,
            },
        },
        "deye_programs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "time": {"type": "string"},
                    "soc_percent": {"type": "number"},
                    "grid_charge": {"type": ["boolean", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["slot", "time", "soc_percent", "grid_charge", "reason"],
                "additionalProperties": False,
            },
        },
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": [
        "summary",
        "confidence",
        "predicted_consumption_rest_of_today_kwh",
        "predicted_consumption_tomorrow_kwh",
        "predicted_pv_tomorrow_kwh",
        "predicted_min_soc_percent",
        "outage_risk",
        "weather_impact",
        "estimated_grid_cost_tomorrow",
        "appliance_forecast",
        "hourly_forecast_tomorrow",
        "deye_programs",
        "recommendations",
        "reasoning",
    ],
    "additionalProperties": False,
}


# Models without the effort parameter (it returns an error there).
NO_EFFORT_MODELS = re.compile(r"claude-(haiku|sonnet-4-5|sonnet-4-0|opus-4-1|opus-4-0|3)")


def supports_effort(model: str) -> bool:
    return not NO_EFFORT_MODELS.search(model)


def result_schema(opts: Options) -> dict[str, Any]:
    """RESULT_SCHEMA with one "<id>_w" column per configured appliance in the hourly forecast."""
    schema = copy.deepcopy(RESULT_SCHEMA)
    hourly = schema["properties"]["hourly_forecast_tomorrow"]["items"]
    appliance_enum = [a.id for a in opts.appliances if a.entity]
    for app_id in appliance_enum:
        hourly["properties"][f"{app_id}_w"] = {"type": "number"}
        hourly["required"].append(f"{app_id}_w")
    if appliance_enum:
        schema["properties"]["appliance_forecast"]["items"]["properties"]["appliance"] = {"type": "string", "enum": appliance_enum}
    return schema


class AnalysisError(Exception):
    pass


def build_input(
    db: Database, opts: Options, snapshot: dict[str, Any], tz: tzinfo, trigger: str = "schedule"
) -> dict[str, Any]:
    now = datetime.fromtimestamp(snapshot["ts"], tz)
    history = db.daily_summary(opts.history_days, now.date())
    since = snapshot["ts"] - opts.history_days * 86400

    weekday_totals = [d["consumption_kwh"] for d in history[:-1] if not d["is_weekend"] and d["consumption_kwh"]]
    weekend_totals = [d["consumption_kwh"] for d in history[:-1] if d["is_weekend"] and d["consumption_kwh"]]

    hourly: dict[str, dict[str, Any]] = {}
    for row in db.readings_since(snapshot["ts"] - RECENT_HOURS * 3600):
        hour = datetime.fromtimestamp(row["ts"], tz).strftime("%Y-%m-%d %H:00")
        hourly[hour] = {  # last sample of each hour
            "hour": hour,
            "soc": row["battery_soc"],
            "load_w": row["load_power"],
            "pv_w": row["pv_power"],
            "appliances_w": row["appliances"],
            "consumption_today_kwh": row["today_consumption"],
            "outdoor_temp": row["outdoor_temp"],
            "target_soc": row["target_soc"],
        }

    report = accuracy_report(db, opts.history_days, now.date(), tz)
    weather = snapshot.get("weather") or {}

    return {
        "plan_day": snapshot.get("plan_day"),
        "recent_outage_days": snapshot.get("recent_outage_days") or [],
        "current": {
            "local_time": snapshot["local_time"],
            "weekday": snapshot["weekday"],
            "is_weekend": snapshot["is_weekend"],
            "battery_soc": snapshot["battery_soc"],
            "solar_forecast_today": snapshot["today_forecast"],
            "solar_forecast_tomorrow": snapshot["tomorrow_forecast"],
            "load_power_w": snapshot["load_power"],
            "pv_power_w": snapshot.get("pv_power"),
            "pv_surplus_w": (
                snapshot["pv_power"] - snapshot["load_power"]
                if snapshot.get("pv_power") is not None and snapshot.get("load_power") is not None
                else None
            ),
            "consumption_today_kwh": snapshot["today_consumption"],
            "pv_today_kwh": snapshot.get("pv_today"),
            "grid_import_today_kwh": snapshot.get("grid_import_today"),
            "appliances_w": snapshot.get("appliances") or {},
            "outdoor_temp": snapshot.get("outdoor_temp"),
            "units": snapshot["units"],
            "outages_state": snapshot["outages_state"],
            "outages_attributes": snapshot["outages_attrs"],
            "active_deye_program": snapshot["active_program_slot"],
            "missing_entities": snapshot["missing_entities"],
        },
        "appliances": [
            {"id": a.id, "name": a.name, "temperature_dependent": a.temperature_dependent}
            for a in opts.appliances
            if a.entity
        ],
        "weather": {key: value for key, value in weather.items() if key != "outdoor_temp"},
        "deye_programs": snapshot["deye_programs"],
        "schedule": {
            "this_run": now.isoformat(timespec="minutes"),
            "trigger": trigger,
            "next_run": opts.next_analysis(now, tz).isoformat(timespec="minutes"),
            "daily_runs": [f"{h:02d}:{m:02d}" for h, m in opts.analysis_times()],
        },
        "weekend_days": opts.weekend_days,
        "averages": {
            "weekday_consumption_kwh": _avg(weekday_totals),
            "weekend_consumption_kwh": _avg(weekend_totals),
            "weekday_days": len(weekday_totals),
            "weekend_days": len(weekend_totals),
        },
        "daily_history": history,
        "monthly_history": monthly_summary(db, now.date()),
        "same_period_last_year": _same_period_last_year(db, now.date()),
        "hourly_profile": db.hourly_profile(since),
        "recent_hourly": list(hourly.values()),
        "accuracy": report,
        "tariff": {**opts.tariff_dict(), "now": opts.tariff_at(now.hour * 60 + now.minute)[1]},
        "tariff_by_hour": [opts.tariff_at(h * 60 + 30)[1] for h in range(24)],
        "tuning": {
            "battery_capacity_kwh": opts.battery_capacity_kwh,
            "prediction_margin_percent": opts.prediction_margin_percent,
            "min_soc_percent": opts.min_soc_percent,
            "max_soc_percent": opts.max_soc_percent,
        },
        "user_notes": opts.extra_instructions or None,
    }


async def analyze(client: anthropic.AsyncAnthropic, opts: Options, data: dict[str, Any]) -> dict[str, Any]:
    """Returns {"result", "model", "input_tokens", "output_tokens"}; raises AnalysisError."""
    user_text = (
        f"Analyse this data and produce the plan. Write all text fields in {opts.response_language}.\n\n"
        + json.dumps(data, ensure_ascii=False, default=str)
    )
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": result_schema(opts)}}
    if supports_effort(opts.claude_model):
        output_config["effort"] = opts.claude_effort
    request: dict[str, Any] = {
        "model": opts.claude_model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_text}],
        "output_config": output_config,
    }

    started = time.monotonic()
    try:
        if opts.claude_model in FALLBACK_MODELS:
            stream_manager = client.beta.messages.stream(**request, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            stream_manager = client.messages.stream(**request)
        async with stream_manager as stream:
            response = await stream.get_final_message()
    except anthropic.AuthenticationError as err:
        raise AnalysisError("Claude rejected the API key. Check it in the Settings tab.") from err
    except anthropic.PermissionDeniedError as err:
        raise AnalysisError(f"The API key has no access to this model or feature: {err.message}") from err
    except anthropic.NotFoundError as err:
        raise AnalysisError(f"Unknown Claude model '{opts.claude_model}': {err.message}") from err
    except anthropic.BadRequestError as err:
        raise AnalysisError(f"Claude rejected the request: {err.message}") from err
    except anthropic.RateLimitError as err:
        raise AnalysisError("Claude rate limit reached; the next scheduled run will try again.") from err
    except anthropic.APIStatusError as err:
        raise AnalysisError(f"Claude API error {err.status_code}: {err.message}") from err
    except anthropic.APIConnectionError as err:
        raise AnalysisError(f"Could not reach the Claude API: {err}") from err
    except anthropic.APIError as err:  # e.g. an error event in the middle of the stream
        raise AnalysisError(f"Claude API error: {err}") from err

    _LOGGER.info(
        "Claude analysis finished in %.1fs (message %s, stop_reason %s)",
        time.monotonic() - started,
        getattr(response, "_request_id", None) or response.id,
        response.stop_reason,
    )
    if response.stop_reason == "refusal":
        raise AnalysisError("Claude declined to answer this request.")
    if response.stop_reason == "max_tokens":
        raise AnalysisError("Claude's answer was cut off (max_tokens reached).")

    text = next((block.text for block in response.content if block.type == "text"), None)
    if not text:
        raise AnalysisError(f"Claude returned no text (stop_reason {response.stop_reason}).")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as err:
        raise AnalysisError(f"Claude returned invalid JSON: {err}") from err

    return {
        "result": result,
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def _same_period_last_year(db: Database, today: date) -> list[dict[str, Any]]:
    """Daily rows from 10 days before to 10 days after this date one year ago."""
    centre = today - timedelta(days=365)
    if all_days(db, today) < 355:
        return []
    window = {(centre + timedelta(days=offset)).isoformat() for offset in range(-10, 11)}
    return [d for d in db.daily_summary(all_days(db, today), today) if d["date"] in window]


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None
