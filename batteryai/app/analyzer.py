"""Builds the analysis input from recorded data and asks Claude for a plan."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, tzinfo
from typing import Any

import anthropic

from config import Options
from db import Database

_LOGGER = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
RECENT_HOURS = 48

SYSTEM_PROMPT = """You are BatteryAI, an energy analyst for a home in Home Assistant with solar panels, a battery and a Deye hybrid inverter.

Each request gives you one JSON document with:
- current: the latest sensor values (battery SOC, solar forecast for today and tomorrow, today's load and consumption counters, the probable-outages sensor with its attributes, local time and weekday).
- deye_programs: the inverter's six time-of-use programs. Each program starts at its time and keeps the battery at or above its SOC capacity until the next program starts; the last program runs until the first one of the next day.
- daily_history: one row per recorded day with total load and consumption, the solar forecast, min/max SOC, weekday and whether it is a weekend.
- recent_hourly: hourly samples from the last 48 hours.
- previous_analyses: your recent predictions, so you can check them against what actually happened.
- user_notes: optional instructions from the owner.

Weekends usually use less energy than weekdays in this home. Check that against daily_history instead of assuming it.

Your tasks:
1. Predict consumption for the rest of today and for tomorrow, using weekday/weekend patterns, recent days, the solar forecast and how accurate your previous predictions were.
2. Judge the outage risk from the outages sensor and make sure the battery will hold enough charge to cover the expected outage windows.
3. Propose an SOC capacity for each Deye program, balancing outage backup, solar self-consumption and grid charging. Keep the owner's program times unless a different time clearly helps, and explain every change.
4. Give short, practical recommendations.

Use the units the sensors report (normally kWh and %). If data is missing, stale or implausible, say so in the summary and lower your confidence; never invent values."""

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "predicted_consumption_rest_of_today_kwh": {"type": "number"},
        "predicted_consumption_tomorrow_kwh": {"type": "number"},
        "predicted_min_soc_percent": {"type": "number"},
        "outage_risk": {"type": "string", "enum": ["none", "low", "medium", "high", "unknown"]},
        "deye_programs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "time": {"type": "string"},
                    "soc_percent": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["slot", "time", "soc_percent", "reason"],
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
        "predicted_min_soc_percent",
        "outage_risk",
        "deye_programs",
        "recommendations",
        "reasoning",
    ],
    "additionalProperties": False,
}


class AnalysisError(Exception):
    pass


def build_input(db: Database, opts: Options, snapshot: dict[str, Any], tz: tzinfo) -> dict[str, Any]:
    now = datetime.fromtimestamp(snapshot["ts"], tz)
    history = db.daily_summary(opts.history_days, now.date())

    weekday_totals = [d["consumption_kwh"] for d in history[:-1] if not d["is_weekend"] and d["consumption_kwh"]]
    weekend_totals = [d["consumption_kwh"] for d in history[:-1] if d["is_weekend"] and d["consumption_kwh"]]

    hourly: dict[str, dict[str, Any]] = {}
    for row in db.readings_since(snapshot["ts"] - RECENT_HOURS * 3600):
        hour = datetime.fromtimestamp(row["ts"], tz).strftime("%Y-%m-%d %H:00")
        hourly[hour] = {  # last sample of each hour
            "hour": hour,
            "soc": row["battery_soc"],
            "today_load": row["today_load"],
            "today_consumption": row["today_consumption"],
            "target_soc": row["target_soc"],
        }

    previous = [
        {
            "time": datetime.fromtimestamp(a["ts"], tz).isoformat(timespec="minutes"),
            "summary": a["summary"],
            "predicted_consumption_rest_of_today_kwh": a["result"].get("predicted_consumption_rest_of_today_kwh"),
            "predicted_consumption_tomorrow_kwh": a["result"].get("predicted_consumption_tomorrow_kwh"),
            "predicted_min_soc_percent": a["result"].get("predicted_min_soc_percent"),
        }
        for a in db.analyses(limit=6)
        if a["status"] == "ok" and a["result"]
    ][:4]

    return {
        "current": {
            "local_time": snapshot["local_time"],
            "weekday": snapshot["weekday"],
            "is_weekend": snapshot["is_weekend"],
            "battery_soc": snapshot["battery_soc"],
            "solar_forecast_today": snapshot["today_forecast"],
            "solar_forecast_tomorrow": snapshot["tomorrow_forecast"],
            "today_load": snapshot["today_load"],
            "today_consumption": snapshot["today_consumption"],
            "units": snapshot["units"],
            "outages_state": snapshot["outages_state"],
            "outages_attributes": snapshot["outages_attrs"],
            "active_deye_program": snapshot["active_program_slot"],
            "missing_entities": snapshot["missing_entities"],
        },
        "deye_programs": snapshot["deye_programs"],
        "weekend_days": opts.weekend_days,
        "averages": {
            "weekday_consumption_kwh": _avg(weekday_totals),
            "weekend_consumption_kwh": _avg(weekend_totals),
            "weekday_days": len(weekday_totals),
            "weekend_days": len(weekend_totals),
        },
        "daily_history": history,
        "recent_hourly": list(hourly.values()),
        "previous_analyses": previous,
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
        raise AnalysisError("Claude rejected the API key. Check claude_api_key in the add-on settings.") from err
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
