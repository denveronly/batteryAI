"""Builds the analysis input from recorded data and asks Claude for a plan."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, tzinfo
from typing import Any

import anthropic

from config import Options
from db import Database, accuracy_report

_LOGGER = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
RECENT_HOURS = 48

SYSTEM_PROMPT = """You are BatteryAI, an energy analyst for a home in Home Assistant with solar panels, a battery and a Deye hybrid inverter. The house is heated by a heat pump when it gets cold, and may also have an electric boiler (water heater) and an EV charger.

Each request gives you one JSON document with:
- current: the latest values: battery SOC, PV power and load power right now (pv_surplus_w > 0 means the battery is being charged by the sun), solar forecast for today and tomorrow, total load power (W), today's consumption counter (kWh), heat pump / boiler / EV power (W), outdoor temperature, the probable-outages sensor with its attributes, local time and weekday.
- weather: the current condition and the forecast for today and tomorrow (daily and, when available, hourly temperatures).
- deye_programs: the inverter's six time-of-use programs. Each program starts at its time and keeps the battery at or above its SOC capacity until the next program starts; the last program runs until the first one of the next day. When a program has grid_charge ("on"/"off"), that is its force-charge switch: when on, the inverter charges the battery from the grid up to the program's SOC.
- schedule: when this plan is applied and when the next run will replace it. Plan for the whole period until the next run.
- daily_history: one row per day with consumption, PV production, grid import, the solar forecast, energy used by each appliance and the hours it was running, outdoor temperatures, min/max SOC, weekday and weekend flag.
- hourly_profile: average power per hour of day for the load and each appliance, split into weekdays and weekends, with the average outdoor temperature for that hour.
- recent_hourly: hourly samples from the last 48 hours.
- accuracy: your earlier predictions compared with what actually happened, as percentages.
- tuning: the owner's settings for planning (safety margin, allowed SOC range).
- tariff: grid prices per kWh with currency; off-peak windows are cheap, every other time is peak.
- user_notes: optional instructions from the owner.

Weekends usually use less energy than weekdays in this home. The heat pump runs more when it is cold, so relate heat pump energy to outdoor temperature in the history and use tomorrow's forecast temperatures to predict it. Check every assumption against the data instead of assuming it.

Your tasks:
1. Predict consumption for the rest of today and for tomorrow, and when each appliance (heat pump, boiler, EV) will run and how much energy it will use. Use weekday/weekend patterns, temperature, the solar forecast and your past accuracy (correct systematic over- or under-prediction).
2. Give an hourly forecast for tomorrow (average W per hour for total load and each appliance).
3. Judge the outage risk from the outages sensor and make sure the battery will hold enough charge to cover the expected outage windows.
4. Propose an SOC capacity for each Deye program. Plan for the predicted consumption increased by tuning.prediction_margin_percent, keep every SOC between tuning.min_soc_percent and tuning.max_soc_percent, balance outage backup, solar self-consumption and grid charging, keep the owner's program times unless a different time clearly helps, and explain every change.
5. Decide force charge (grid_charge) for each program that has a switch. Turn it on and raise the SOC before an expected outage when the battery would otherwise not cover the load until power returns, taking into account the time of day: if the outage falls in daylight hours and the PV forecast covers the load and recharges the battery, grid charging is not needed. Turn it off when PV is expected to be enough, so the battery is charged by the sun. For programs without a switch return null.
6. Minimise what is paid for grid energy: charge from the grid in off-peak windows rather than peak, use PV first, and cover peak-time load from the battery. Estimate tomorrow's grid cost in the tariff currency.
7. Give short, practical recommendations.

Use the units the sensors report (W for power, kWh for energy, % for SOC). If data is missing, stale or implausible, say so in the summary and lower your confidence; never invent values. If an appliance sensor is not configured, return 0 for it."""

APPLIANCE_NAMES = ["heat_pump", "boiler", "ev"]

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
                    "appliance": {"type": "string", "enum": APPLIANCE_NAMES},
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
                    "heat_pump_w": {"type": "number"},
                    "boiler_w": {"type": "number"},
                    "ev_w": {"type": "number"},
                },
                "required": ["hour", "load_w", "heat_pump_w", "boiler_w", "ev_w"],
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
            "heat_pump_w": row["heat_pump_power"],
            "boiler_w": row["boiler_power"],
            "ev_w": row["ev_power"],
            "consumption_today_kwh": row["today_consumption"],
            "outdoor_temp": row["outdoor_temp"],
            "target_soc": row["target_soc"],
        }

    report = accuracy_report(db, opts.history_days, now.date(), tz)
    weather = snapshot.get("weather") or {}

    return {
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
            "heat_pump_power_w": snapshot.get("heat_pump_power"),
            "boiler_power_w": snapshot.get("boiler_power"),
            "ev_power_w": snapshot.get("ev_power"),
            "outdoor_temp": snapshot.get("outdoor_temp"),
            "units": snapshot["units"],
            "outages_state": snapshot["outages_state"],
            "outages_attributes": snapshot["outages_attrs"],
            "active_deye_program": snapshot["active_program_slot"],
            "missing_entities": snapshot["missing_entities"],
        },
        "configured_appliances": [
            name for name, entity in (
                ("heat_pump", opts.heat_pump_power_sensor),
                ("boiler", opts.boiler_power_sensor),
                ("ev", opts.ev_power_sensor),
            ) if entity
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
        "hourly_profile": db.hourly_profile(since),
        "recent_hourly": list(hourly.values()),
        "accuracy": report,
        "tariff": {**opts.tariff_dict(), "now": opts.tariff_at(now.hour * 60 + now.minute)[1]},
        "tuning": {
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
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": RESULT_SCHEMA}}
    if "haiku" not in opts.claude_model:
        output_config["effort"] = opts.claude_effort
    request: dict[str, Any] = {
        "model": opts.claude_model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_text}],
        "output_config": output_config,
    }

    started = time.monotonic()
    try:
        if opts.claude_model in FALLBACK_MODELS:
            response = await client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            response = await client.messages.create(**request)
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

    _LOGGER.info(
        "Claude analysis finished in %.1fs (request %s, stop_reason %s)",
        time.monotonic() - started,
        response._request_id,
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


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None
