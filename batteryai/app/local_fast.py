"""Local fast engine: statistical forecast + rule-based SOC planner (no LLM, light on CPU).

Forecast: tomorrow's hourly load is the average of the most similar recorded days (same
weekday/weekend type, closest outdoor temperature, recent days preferred). Appliances
marked temperature-dependent (heat pump, AC) are scaled with a temperature regression. PV is the recorded PV power shape scaled to the
solar forecast.

Planner: for each Deye program range it estimates how much energy the battery must hold
for the following more expensive tariff hours that PV will not cover, adds the safety margin, and
turns that into an SOC target; grid charge is switched on in cheapest-tariff programs when PV
will not refill the battery, and before outages.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, tzinfo
from typing import Any

from collector import program_ranges
from config import WEEKDAYS, Options
from db import Database

SIMILAR_DAYS = 5
ACTIVE_W = 200
OUTAGE_OFF_STATES = {"off", "0", "false", "none", "no", "unknown", "unavailable", ""}


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _day_profiles(db: Database, tz: tzinfo, since_ts: int) -> dict[str, dict[str, Any]]:
    """date -> {"weekday", "is_weekend", "hours": {h: {field: avg}}, "temp"}; appliances as "app:<id>"."""
    fields = ["load_power", "pv_power", "outdoor_temp"]
    buckets: dict[str, dict[int, dict[str, list[float]]]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for row in db.readings_since(since_ts):
        day = row["local_date"]
        meta.setdefault(day, {"weekday": row["weekday"], "is_weekend": bool(row["is_weekend"])})
        hour = buckets.setdefault(day, {}).setdefault(row["minute_of_day"] // 60, {f: [] for f in fields})
        for f in fields:
            if row[f] is not None:
                hour[f].append(row[f])
        for app_id, watts in row["appliances"].items():
            if watts is not None:
                hour.setdefault(f"app:{app_id}", []).append(watts)
    profiles = {}
    for day, hours in buckets.items():
        averaged = {h: {f: _mean(v) for f, v in values.items()} for h, values in hours.items()}
        if sum(1 for h in averaged.values() if h["load_power"] is not None) < 18:
            continue  # incomplete day (today, or a gap in recording)
        profiles[day] = {
            **meta[day],
            "hours": averaged,
            "temp": _mean([h["outdoor_temp"] for h in averaged.values()]),
        }
    return profiles


def _kwh(profile_hours: dict[int, dict[str, Any]], field: str) -> float:
    return sum((h.get(field) or 0) for h in profile_hours.values()) / 1000


def _similar(profiles: dict[str, dict[str, Any]], is_weekend: bool, temp: float | None, today: str) -> list[str]:
    target = datetime.fromisoformat(today)

    def score(day: str) -> float:
        p = profiles[day]
        when = datetime.fromisoformat(day)
        age = (target - when).days
        # Days from the same time of year count as "seasonally close" even a year ago.
        season_gap = min(abs(target.timetuple().tm_yday - when.timetuple().tm_yday), 366 - abs(target.timetuple().tm_yday - when.timetuple().tm_yday))
        s = 0.0 if p["is_weekend"] == is_weekend else 6.0
        if temp is not None and p["temp"] is not None:
            s += abs(p["temp"] - temp)
        return s + 0.03 * min(age, 60) + 0.08 * season_gap

    return sorted(profiles, key=score)[:SIMILAR_DAYS]


def _temperature_regression(profiles: dict[str, dict[str, Any]], field: str) -> tuple[float, float] | None:
    """Daily appliance kWh = a + b * average temperature (least squares)."""
    points = [
        (p["temp"], _kwh(p["hours"], field))
        for p in profiles.values()
        if p["temp"] is not None and any(h.get(field) is not None for h in p["hours"].values())
    ]
    if len(points) < 5:
        return None
    mean_t = sum(t for t, _ in points) / len(points)
    mean_e = sum(e for _, e in points) / len(points)
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var < 1:
        return None
    slope = sum((t - mean_t) * (e - mean_e) for t, e in points) / var
    return mean_e - slope * mean_t, slope


def _windows(hours: list[int]) -> str:
    if not hours:
        return "not expected"
    spans, start, prev = [], hours[0], hours[0]
    for h in hours[1:] + [None]:
        if h is not None and h == prev + 1:
            prev = h
            continue
        spans.append(f"{start:02d}–{prev + 1:02d}")
        if h is not None:
            start = prev = h
    return ", ".join(spans)


def _pv_shape(profiles: dict[str, dict[str, Any]]) -> list[float]:
    """Share of the day's PV per hour, from recorded PV power (or a daylight curve)."""
    sums = [0.0] * 24
    for p in profiles.values():
        for h, values in p["hours"].items():
            sums[h] += values.get("pv_power") or 0
    total = sum(sums)
    if total <= 0:
        sums = [max(0.0, math.sin((h + 0.5 - 6) / 14 * math.pi)) if 6 <= h < 20 else 0.0 for h in range(24)]
        total = sum(sums)
    return [s / total for s in sums]


def forecast(db: Database, opts: Options, snapshot: dict[str, Any], tz: tzinfo) -> dict[str, Any]:
    now = datetime.fromtimestamp(snapshot["ts"], tz)
    tomorrow = (now + timedelta(days=1)).date()
    # All stored history: last year's days from the same season are good matches too.
    profiles = _day_profiles(db, tz, snapshot["ts"] - max(opts.history_days, 400) * 86400)
    weather = snapshot.get("weather") or {}
    tomorrow_weather = weather.get("tomorrow") or {}
    temps = [t for t in (tomorrow_weather.get("temperature"), tomorrow_weather.get("templow")) if t is not None]
    tomorrow_temp = sum(temps) / len(temps) if temps else snapshot.get("outdoor_temp")
    tomorrow_weekend = WEEKDAYS[tomorrow.weekday()] in opts.weekend_days

    similar = _similar(profiles, tomorrow_weekend, tomorrow_temp, now.date().isoformat())
    appliances = [a for a in opts.appliances if a.entity]
    hourly = []
    for h in range(24):
        entry = {"hour": h}
        for key, field in (("load_w", "load_power"), *((f"{a.id}_w", f"app:{a.id}") for a in appliances)):
            entry[key] = round(_mean([profiles[d]["hours"].get(h, {}).get(field) for d in similar]) or 0)
        hourly.append(entry)

    notes = []
    for appliance in appliances:
        if not appliance.temperature_dependent or tomorrow_temp is None:
            continue
        key = f"{appliance.id}_w"
        regression = _temperature_regression(profiles, f"app:{appliance.id}")
        if not regression:
            continue
        a, b = regression
        expected = max(0.0, a + b * tomorrow_temp)
        current = sum(e[key] for e in hourly) / 1000
        if current > 0.1:
            factor = expected / current
            for e in hourly:
                new_w = e[key] * factor
                e["load_w"] = round(max(0, e["load_w"] + new_w - e[key]))  # the appliance is part of the load
                e[key] = round(new_w)
        notes.append(f"{appliance.name} is expected to use {expected:.1f} kWh ({b:+.2f} kWh per degree in your history)")
    if tomorrow_temp is None:
        weather_note = "No temperature data; appliance use taken from similar days."
    elif notes:
        weather_note = f"Tomorrow about {tomorrow_temp:.1f}°: " + "; ".join(notes) + "."
    else:
        weather_note = f"Tomorrow about {tomorrow_temp:.1f}°; days with similar temperature were used."

    pv_total = snapshot.get("tomorrow_forecast") or 0
    shape = _pv_shape(profiles)
    pv_hourly = [round(pv_total * 1000 * s) for s in shape]

    # Rest of today from today's similar days.
    today_similar = _similar(profiles, snapshot["is_weekend"], snapshot.get("outdoor_temp"), now.date().isoformat())
    rest_today = sum(
        (_mean([profiles[d]["hours"].get(h, {}).get("load_power") for d in today_similar]) or 0)
        for h in range(now.hour, 24)
    ) / 1000

    return {
        "hourly": hourly,
        "pv_hourly": pv_hourly,
        "similar_days": similar,
        "tomorrow_temp": tomorrow_temp,
        "weather_note": weather_note,
        "consumption_tomorrow_kwh": round(sum(e["load_w"] for e in hourly) / 1000, 1),
        "rest_of_today_kwh": round(rest_today, 1),
        "pv_tomorrow_kwh": round(pv_total, 1),
    }


def _outage_expected(snapshot: dict[str, Any]) -> bool:
    state = str(snapshot.get("outages_state") or "").strip().lower()
    if state in OUTAGE_OFF_STATES:
        return False
    try:
        return float(state) > 0
    except ValueError:
        return True


def plan(opts: Options, snapshot: dict[str, Any], fc: dict[str, Any]) -> dict[str, Any]:
    capacity = max(1.0, opts.battery_capacity_kwh)
    margin = 1 + opts.prediction_margin_percent / 100
    load = [e["load_w"] for e in fc["hourly"]]
    pv = fc["pv_hourly"]
    deficit = [max(0, load[h] - pv[h]) / 1000 for h in range(24)]  # kWh the grid/battery must cover
    surplus = [max(0, pv[h] - load[h]) / 1000 for h in range(24)]
    # "Cheap" hours are those of the cheapest tariff: the time to charge from the grid.
    offpeak = [opts.is_cheap(h * 60 + 30) for h in range(24)]
    prices = [opts.tariff_at(h * 60 + 30)[0] for h in range(24)]
    names = [opts.tariff_at(h * 60 + 30)[1] for h in range(24)]
    outage = _outage_expected(snapshot)
    pv_short = fc["pv_tomorrow_kwh"] < fc["consumption_tomorrow_kwh"] * 0.8

    programs = snapshot.get("deye_programs") or []
    ranges = program_ranges(programs, opts.program_time_marks == "end")
    results = []
    for program in programs:
        slot = program["slot"]
        span = ranges.get(slot)
        has_switch = program.get("grid_charge") is not None
        if not span or span[0] == span[1]:
            results.append({
                "slot": slot, "time": program.get("time") or "", "soc_percent": program.get("soc") or opts.min_soc_percent,
                "grid_charge": None, "reason": "Unused (same time as the previous program); left unchanged.",
            })
            continue
        duration = (span[1] - span[0]) % 1440
        hours = sorted({((span[0] + m) // 60) % 24 for m in range(0, duration, 15)}, key=lambda h: (h - span[0] // 60) % 24)
        mid = ((span[0] + duration // 2) // 60) % 24
        end_h = (span[1] // 60) % 24
        if opts.single_price:
            daytime_pv = sum(surplus[h] for h in hours)
            soc = opts.min_soc_percent
            grid = False
            reason = (
                "Single price all day: charging from the grid saves nothing, the battery stores PV"
                + (f" (~{daytime_pv:.1f} kWh surplus in this window)." if daytime_pv > 0.5 else " and covers the load.")
            )
        elif offpeak[mid]:
            # Charge window: hold enough for the peak hours until the next off-peak period,
            # minus half of the PV surplus expected in between (it can recharge the battery).
            need, refill, h = 0.0, 0.0, end_h
            for _ in range(24):  # skip the rest of the off-peak period
                if not offpeak[h]:
                    break
                h = (h + 1) % 24
            for _ in range(24):  # then add up the peak hours until off-peak starts again
                if offpeak[h]:
                    break
                need += deficit[h]
                refill += surplus[h]
                h = (h + 1) % 24
            energy = max(0.0, need - 0.5 * refill) * margin
            soc = opts.min_soc_percent + energy / capacity * 100
            grid = pv_short or energy > 0.2 * capacity
            reason = (
                f"Cheapest tariff ({names[mid]}). The following pricier hours need ~{need:.1f} kWh, PV can add "
                f"~{refill:.1f} kWh; keeping {energy:.1f} kWh (+{opts.prediction_margin_percent}% margin) in the battery."
            )
        else:
            daytime_pv = sum(surplus[h] for h in hours)
            soc = opts.min_soc_percent
            grid = False
            reason = (
                f"{names[mid]} tariff: use the battery. PV surplus in this window ~{daytime_pv:.1f} kWh recharges it."
                if daytime_pv > 0.5
                else f"{names[mid]} tariff: use the battery down to the minimum SOC."
            )
        if outage:
            soc = max(soc, opts.max_soc_percent)
            grid = True
            reason += " Outage expected: keep the battery full."
        soc = round(min(opts.max_soc_percent, max(opts.min_soc_percent, soc)))
        results.append({
            "slot": slot,
            "time": program.get("time") or "",
            "soc_percent": soc,
            "grid_charge": grid if has_switch else None,
            "reason": reason,
        })

    # Rough grid cost: deficits in cheap hours bought directly, battery energy charged at the
    # cheapest price, and whatever the battery can't cover bought at the price of its hour.
    cheap_price = opts.cheapest_price
    cost = sum(deficit[h] * prices[h] for h in range(24) if offpeak[h])
    stored = sum(
        max(0, r["soc_percent"] - opts.min_soc_percent) / 100 * capacity
        for r, p in zip(results, programs) if r["grid_charge"]
    )
    peak_deficit = sum(deficit[h] for h in range(24) if not offpeak[h])
    peak_price = (
        sum(deficit[h] * prices[h] for h in range(24) if not offpeak[h]) / peak_deficit if peak_deficit else cheap_price
    )
    cost += min(stored, peak_deficit) * cheap_price + max(0.0, peak_deficit - stored) * peak_price
    return {"programs": results, "outage": outage, "pv_short": pv_short, "cost": round(cost, 2)}


def analyze(db: Database, opts: Options, snapshot: dict[str, Any], tz: tzinfo) -> dict[str, Any]:
    fc = forecast(db, opts, snapshot, tz)
    p = plan(opts, snapshot, fc)
    hourly = fc["hourly"]
    appliance_forecast = []
    recommendations = []
    for appliance in (a for a in opts.appliances if a.entity):
        key = f"{appliance.id}_w"
        active = [e["hour"] for e in hourly if e[key] >= ACTIVE_W]
        appliance_forecast.append({
            "appliance": appliance.id,
            "expected_kwh_tomorrow": round(sum(e[key] for e in hourly) / 1000, 1),
            "expected_usage_windows": _windows(active),
            "reason": "Average of the most similar recorded days"
            + (", adjusted for temperature." if appliance.temperature_dependent else "."),
        })
        peak_hours = [h for h in active if not opts.is_cheap(h * 60 + 30)]
        if peak_hours and not appliance.temperature_dependent:
            cheapest = opts.tariff_dict()["cheapest"]
            recommendations.append(f"If possible, move {appliance.name} from pricier hours ({_windows(peak_hours)}) to the {cheapest} tariff.")
    if p["pv_short"] and opts.single_price:
        recommendations.append("PV will not cover tomorrow's use: with a single price, run flexible loads in the sunniest hours.")
    elif p["pv_short"]:
        recommendations.append(f"PV will not cover tomorrow's use: charge from the grid during the {opts.tariff_dict()['cheapest']} tariff.")
    else:
        recommendations.append("PV should cover most of tomorrow: run flexible loads in the sunniest hours.")

    days = len(fc["similar_days"])
    confidence = "medium" if days >= 4 else "low"
    min_soc = min((r["soc_percent"] for r in p["programs"]), default=opts.min_soc_percent)
    summary = (
        f"Tomorrow ~{fc['consumption_tomorrow_kwh']} kWh use vs ~{fc['pv_tomorrow_kwh']} kWh PV"
        + (", outage expected – battery kept full" if p["outage"] else "")
        + f". Based on {days} similar day(s)."
    )
    reasoning = (
        "Local fast engine (statistical, no AI). Similar days used: "
        + (", ".join(fc["similar_days"]) or "none")
        + f". {fc['weather_note']} PV hourly shape from recorded PV power, scaled to the forecast. "
        "Programs in the cheapest tariff keep enough energy for the following pricier hours not covered by PV; "
        "programs in pricier tariffs let the battery discharge to the minimum SOC."
    )
    return {
        "summary": summary,
        "confidence": confidence,
        "predicted_consumption_rest_of_today_kwh": fc["rest_of_today_kwh"],
        "predicted_consumption_tomorrow_kwh": fc["consumption_tomorrow_kwh"],
        "predicted_pv_tomorrow_kwh": fc["pv_tomorrow_kwh"],
        "predicted_min_soc_percent": min_soc,
        "outage_risk": "high" if p["outage"] else ("unknown" if not (opts.outage_minutes_sensor or opts.emergency_outage_sensor) else "low"),
        "weather_impact": fc["weather_note"],
        "estimated_grid_cost_tomorrow": p["cost"],
        "appliance_forecast": appliance_forecast,
        "hourly_forecast_tomorrow": hourly,
        "_pv_hourly": fc["pv_hourly"],
        "deye_programs": p["programs"],
        "recommendations": recommendations,
        "reasoning": reasoning,
    }
