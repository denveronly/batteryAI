"""Shared planning rules for every prediction engine.

- Plan day: a prediction run before PLAN_TODAY_BEFORE_HOUR plans today (its programs are
  still ahead), a later run plans tomorrow.
- Night reserve: when outages are likely (an outage is scheduled or emergency outages are
  on now, or outages were recorded in the last days), the programs that run in the evening
  and night – when there is no PV – are kept at the night reserve SOC, so the battery is
  full when the sun is gone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

from collector import program_ranges
from config import WEEKDAYS, Options

PLAN_TODAY_BEFORE_HOUR = 13
OUTAGE_LOOKBACK_DAYS = 7
NIGHT_PV_W = 100  # an hour with less average PV than this is night
DEFAULT_NIGHT = set(range(18, 24)) | set(range(0, 7))


def plan_day(snapshot: dict[str, Any], opts: Options, tz: tzinfo) -> dict[str, Any]:
    now = datetime.fromtimestamp(snapshot["ts"], tz)
    is_today = now.hour < PLAN_TODAY_BEFORE_HOUR
    day = now.date() if is_today else (now + timedelta(days=1)).date()
    weekday = WEEKDAYS[day.weekday()]
    weather = snapshot.get("weather") or {}
    return {
        "date": day.isoformat(),
        "label": "today" if is_today else "tomorrow",
        "weekday": weekday,
        "is_weekend": weekday in opts.weekend_days,
        "solar_forecast_kwh": snapshot.get("today_forecast" if is_today else "tomorrow_forecast"),
        "weather": weather.get("today" if is_today else "tomorrow"),
    }


def night_hours(profile: list[dict[str, Any]]) -> set[int]:
    """Hours of the day without useful PV, from the recorded average PV power per hour."""
    pv: dict[int, list[float]] = {}
    for row in profile:
        if row.get("pv_power") is not None:
            pv.setdefault(row["hour"], []).append(row["pv_power"])
    if len(pv) < 20:
        return set(DEFAULT_NIGHT)
    return {h for h in range(24) if sum(pv.get(h, [0])) / len(pv.get(h, [0])) < NIGHT_PV_W}


def recent_outage_days(db: Any, now_ts: int) -> list[str]:
    """Days in the last week on which an outage was scheduled or emergency outages were on."""
    since = now_ts - OUTAGE_LOOKBACK_DAYS * 86400
    rows = db._query(
        "SELECT DISTINCT local_date FROM readings WHERE ts >= ? AND lower(outages_state) IN ('on', 'emergency') "
        "ORDER BY local_date",
        (since,),
    )
    return [r["local_date"] for r in rows]


def outages_likely(snapshot: dict[str, Any], outage_days: list[str]) -> str | None:
    """Why outages are likely tonight, or None."""
    attrs = snapshot.get("outages_attrs") or {}
    if attrs.get("emergency_outages"):
        return "emergency outages are on"
    if attrs.get("scheduled_outage"):
        return f"an outage is scheduled ({attrs.get('outage_starts') or 'soon'})"
    if outage_days:
        return f"outages on {len(outage_days)} of the last {OUTAGE_LOOKBACK_DAYS} days"
    return None


def _profile_w(profile: list[dict[str, Any]], key: str) -> dict[tuple[int, int], float]:
    return {(p["is_weekend"], p["hour"]): p[key] for p in profile if p.get(key) is not None}


def _avg_w(values: dict[tuple[int, int], float], local: datetime, opts: Options, default: float) -> float:
    weekend = int(WEEKDAYS[local.weekday()] in opts.weekend_days)
    value = values.get((weekend, local.hour), values.get((1 - weekend, local.hour)))
    return default if value is None else value


def _net_steps(
    opts: Options, tz: tzinfo, loads: dict, pv: dict, pv_scale: float, start: float, end: float
) -> list[float]:
    """kWh into (+) or out of (−) the battery per 15 minutes between two moments, from the
    recorded average load and PV per hour (PV scaled to the solar forecast)."""
    steps, ts = [], start
    while ts < end:
        step = min(900, end - ts)
        local = datetime.fromtimestamp(ts, tz)
        load = _avg_w(loads, local, opts, 1000.0)
        sun = _avg_w(pv, local, opts, 0.0) * pv_scale
        steps.append((sun - load) * step / 3_600_000)
        ts += step
    return steps


def _simulate(stored: float, steps: list[float], headroom: float) -> tuple[float, float]:
    """(energy at the end, lowest level) of the battery above the minimum SOC; the sun can
    only fill it up to the maximum SOC."""
    level = low = stored
    for delta in steps:
        level = min(headroom, level + delta)
        low = min(low, level)
        level = max(0.0, level)  # empty: the grid covers the rest
    return level, low


def _next_start(now: datetime, minute: int) -> float:
    """The next time a program starting at minute of day begins (now if it is running)."""
    today = now.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
    return today.timestamp() if today >= now else (today + timedelta(days=1)).timestamp()


def survives_until_cheap(
    opts: Options, tz: tzinfo, now_ts: float, soc: float | None, program_start: int, program_running: bool,
    profile: list[dict[str, Any]], solar_forecast_kwh: float | None, cheap_soc: float | None = None,
) -> dict[str, Any] | None:
    """Several tariffs, a night program in a pricier tariff that comes before the next cheap
    tariff: with the battery as it is now, the sun until then and the expected consumption
    (plus the margin), does it last until the cheapest tariff begins? None when the program
    only starts after the next cheap period (a later prediction decides it)."""
    from outage_plan import next_cheap  # outage_plan imports collector, like this module

    now = datetime.fromtimestamp(now_ts, tz)
    # The horizon: from now (or, in the cheap tariff now, from its end – the battery is
    # charged there to the cheap programs' SOC) until the next cheap tariff begins.
    origin = now_ts
    if opts.is_cheap(now.hour * 60 + now.minute):
        origin = now_ts
        while origin < now_ts + 86400 and opts.is_cheap(datetime.fromtimestamp(origin, tz).hour * 60 + datetime.fromtimestamp(origin, tz).minute):
            origin += 60
    if origin > now_ts:
        program_running = False  # its pricier part is over; the next occurrence counts
    start = max(origin, now_ts if program_running else _next_start(now, program_start))
    if not program_running and _next_start(datetime.fromtimestamp(origin, tz), program_start) > next_cheap(opts, tz, origin):
        return None  # starts after the next cheap period: a later prediction decides it
    start = max(origin, _next_start(datetime.fromtimestamp(origin, tz), program_start)) if not program_running else start
    cheap_at = next_cheap(opts, tz, start)
    loads, pv = _profile_w(profile, "load_power"), _profile_w(profile, "pv_power")
    day_pv = sum(_avg_w(pv, datetime(2026, 1, 5, h), opts, 0.0) for h in range(24)) / 1000  # a weekday
    pv_scale = (solar_forecast_kwh / day_pv) if solar_forecast_kwh and day_pv > 0.5 else 1.0
    margin = 1 + opts.prediction_margin_percent / 100
    capacity = opts.battery_capacity_kwh
    headroom = (opts.max_soc_percent - opts.min_soc_percent) / 100 * capacity
    stored = max(0.0, ((soc if soc is not None else opts.min_soc_percent) - opts.min_soc_percent) / 100 * capacity)
    if origin > now_ts:  # cheap now: what the night leaves, at least the cheap programs' SOC
        stored = max(_simulate(stored, _net_steps(opts, tz, loads, pv, pv_scale, now_ts, origin), headroom)[0], 0.0)
        if cheap_soc is not None:
            stored = max(stored, (cheap_soc - opts.min_soc_percent) / 100 * capacity)
    before = _net_steps(opts, tz, loads, pv, pv_scale, origin, start)
    during = [d * margin if d < 0 else d for d in _net_steps(opts, tz, loads, pv, pv_scale, start, cheap_at)]
    available, _ = _simulate(stored, before, headroom)
    # The smallest energy at the program's start that never runs out before the cheap tariff.
    lo, hi = 0.0, headroom
    for _ in range(25):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if _simulate(mid, during, headroom)[1] >= 0 else (mid, hi)
    need = hi
    # In the cheap tariff now: the energy the cheap hours must leave in the battery instead.
    whole = before + during
    lo, hi = 0.0, headroom
    for _ in range(25):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if _simulate(mid, whole, headroom)[1] >= 0 else (mid, hi)
    return {
        "cheap_now": origin > now_ts,
        "soc_needed_from_cheap": min(opts.max_soc_percent, round(opts.min_soc_percent + hi / capacity * 100)),
        "survives": available + 1e-6 >= need,
        "available_kwh": round(max(0.0, available), 1),
        "need_kwh": round(need, 1),
        "cheap_at": datetime.fromtimestamp(cheap_at, tz).strftime("%H:%M"),
        "soc_needed": min(opts.max_soc_percent, round(opts.min_soc_percent + need / capacity * 100)),
    }


def apply_night_reserve(
    result: dict[str, Any], opts: Options, snapshot: dict[str, Any], night: set[int], reason: str | None,
    keep_grid_charge: bool, profile: list[dict[str, Any]] | None = None, now_ts: float | None = None,
    tz: tzinfo | None = None,
) -> list[int]:
    """Evening/night programs (mostly hours without PV):
    - one tariff: grid charge always stays on, so the battery can recharge before an
      unplanned emergency outage;
    - several tariffs: on in programs of the cheapest tariff; in pricier ones only when the
      battery would not last until the cheapest tariff (consumption and PV), then with the
      SOC that needs;
    - when outages are likely (reason), their SOC is raised to the night reserve.
    Nothing touches grid charge when it is left alone. Returns the slots it changed."""
    reserve = min(opts.night_reserve_soc_percent, opts.max_soc_percent) if reason else 0
    times = {p["slot"]: p.get("time") for p in snapshot.get("deye_programs") or []}
    programs = [
        {**p, "time": times.get(p.get("slot")) or p.get("time")}
        for p in result.get("deye_programs") or [] if isinstance(p, dict) and p.get("slot") is not None
    ]
    ranges = program_ranges(programs, opts.program_time_marks == "end")
    now_ts = now_ts if now_ts is not None else snapshot.get("ts")
    now_minute = None
    if tz is not None and now_ts is not None:
        local_now = datetime.fromtimestamp(now_ts, tz)
        now_minute = local_now.hour * 60 + local_now.minute
    def span_minutes(span: tuple[int, int]) -> list[int]:
        start, end = span
        return list(range(start, end if end > start else end + 1440, 60))

    # The SOC the cheapest-tariff programs charge to from the grid (several tariffs).
    cheap_socs = [
        p.get("soc_percent") for p in result.get("deye_programs") or []
        if ranges.get(p.get("slot")) and ranges[p["slot"]][0] != ranges[p["slot"]][1]
        and p.get("grid_charge") is not False and p.get("soc_percent") is not None
        and sum(opts.is_cheap(m % 1440) for m in span_minutes(ranges[p["slot"]])) * 2 >= len(span_minutes(ranges[p["slot"]]))
    ]
    cheap_soc = max(cheap_socs) if cheap_socs else None
    cheap_slots = [
        p["slot"] for p in result.get("deye_programs") or []
        if ranges.get(p.get("slot")) and ranges[p["slot"]][0] != ranges[p["slot"]][1] and p.get("grid_charge") is not None
        and sum(opts.is_cheap(m % 1440) for m in span_minutes(ranges[p["slot"]])) * 2 >= len(span_minutes(ranges[p["slot"]]))
    ] if not opts.single_price and not keep_grid_charge else []
    cheap_raise = 0
    changed, raised = [], []
    for program in result.get("deye_programs") or []:
        span = ranges.get(program.get("slot"))
        if not span or span[0] == span[1]:
            continue
        start, end = span
        minutes = span_minutes(span)
        hours = {(m // 60) % 24 for m in minutes}
        if not hours or len(hours & night) * 2 < len(hours):
            continue  # mostly daytime: PV charges the battery there
        notes = []
        if reserve and (program.get("soc_percent") is None or program["soc_percent"] < reserve):
            program["soc_percent"] = reserve
            notes.append(f"Night reserve {reserve}%: {reason}.")
            raised.append(program["slot"])
        if not keep_grid_charge and program.get("grid_charge") is not None:
            if opts.single_price:
                if program["grid_charge"] is False:
                    program["grid_charge"] = True
                    notes.append("Grid charge stays on at night, so the battery can recharge for an unplanned outage.")
            elif sum(opts.is_cheap(m % 1440) for m in minutes) * 2 >= len(minutes):
                if program["grid_charge"] is False:
                    program["grid_charge"] = True
                    notes.append("Cheapest tariff at night: grid charge on.")
            elif profile is not None and tz is not None and now_ts is not None:
                running = now_minute is not None and (
                    start <= now_minute < end if start < end else now_minute >= start or now_minute < end
                )
                check = survives_until_cheap(
                    opts, tz, now_ts, snapshot.get("battery_soc"), start, running, profile,
                    (snapshot.get("plan_day") or {}).get("solar_forecast_kwh"), cheap_soc,
                )
                if check is None:
                    pass  # starts after the next cheap period: the next prediction decides it
                elif check["survives"]:
                    if program["grid_charge"]:
                        program["grid_charge"] = False
                        notes.append(
                            f"Pricier tariff: the battery (~{check['available_kwh']} kWh) lasts until the cheap tariff at "
                            f"{check['cheap_at']} (~{check['need_kwh']} kWh needed), so no grid charge."
                        )
                elif check["cheap_now"] and cheap_slots:
                    # Cheaper: charge more in tonight's cheap hours instead of in the pricier tariff.
                    cheap_raise = max(cheap_raise, check["soc_needed_from_cheap"])
                    if program["grid_charge"]:
                        program["grid_charge"] = False
                    notes.append(
                        f"Pricier tariff: the battery would not last until the cheap tariff at {check['cheap_at']} "
                        f"(~{check['need_kwh']} kWh needed), so the cheap hours tonight charge it to "
                        f"{check['soc_needed_from_cheap']}% instead of charging here."
                    )
                else:
                    program["grid_charge"] = True
                    program["soc_percent"] = max(program.get("soc_percent") or 0, check["soc_needed"])
                    notes.append(
                        f"Pricier tariff, but the battery (~{check['available_kwh']} kWh) would not last until the cheap "
                        f"tariff at {check['cheap_at']} (~{check['need_kwh']} kWh needed): grid charge on to "
                        f"{program['soc_percent']}%."
                    )
        if notes:
            program["reason"] = f"{program.get('reason') or ''} {' '.join(notes)}".strip()
            changed.append(program["slot"])
    if cheap_raise:
        for program in result.get("deye_programs") or []:
            if program.get("slot") in cheap_slots and (program.get("soc_percent") or 0) < cheap_raise:
                program["soc_percent"] = cheap_raise
                program["grid_charge"] = True
                program["reason"] = (
                    f"{program.get('reason') or ''} Cheapest tariff: charged to {cheap_raise}% so the battery lasts "
                    "through the pricier hours until the next cheap tariff."
                ).strip()
                if program["slot"] not in changed:
                    changed.append(program["slot"])
    if raised:
        slots = ", ".join(f"P{s}" for s in raised)
        result["summary"] = f"{result.get('summary') or ''} Night reserve: {slots} at {reserve}% ({reason}).".strip()
    return changed


def _slots_between(ranges: dict[int, tuple[int, int]], start: datetime, end: datetime) -> list[int]:
    """Programs in effect at any moment from start to end (at most a day)."""
    slots: list[int] = []
    t = start
    while t <= end:
        minute = t.hour * 60 + t.minute
        for slot, (a, b) in ranges.items():
            if a != b and (a <= minute < b if a < b else minute >= a or minute < b) and slot not in slots:
                slots.append(slot)
        t += timedelta(minutes=15)
    return slots


def apply_expected_outages(
    result: dict[str, Any], opts: Options, snapshot: dict[str, Any], windows: list[dict[str, Any]],
    profile: list[dict[str, Any]], keep_grid_charge: bool, tz: tzinfo, strict: str | None = None,
) -> list[int]:
    """Outage windows expected on the plan day (yesterday's outages repeat, give or take
    outage_shift_hours): every program from the earliest possible start until the latest
    possible end holds the SOC the window needs (recorded load per hour plus the margin;
    the maximum SOC when strict) with grid charge on, so the battery is ready whenever the
    grid goes. Returns the slots it changed."""
    from outage_plan import energy_kwh, load_profile  # outage_plan imports collector, like this module

    if not windows:
        return []
    loads = load_profile(profile)
    times = {p["slot"]: p.get("time") for p in snapshot.get("deye_programs") or []}
    programs = [
        {**p, "time": times.get(p.get("slot")) or p.get("time")}
        for p in result.get("deye_programs") or [] if isinstance(p, dict) and p.get("slot") is not None
    ]
    ranges = program_ranges(programs, opts.program_time_marks == "end")
    by_slot = {p.get("slot"): p for p in result.get("deye_programs") or []}
    margin = 1 + opts.prediction_margin_percent / 100
    now_ts = float(snapshot.get("ts") or 0)
    changed = []
    for window in windows:
        need = energy_kwh(loads, opts, tz, window["start"], window["end"]) * margin
        target = min(opts.max_soc_percent, round(opts.min_soc_percent + need / opts.battery_capacity_kwh * 100))
        if strict:
            target = opts.max_soc_percent
        shift = window.get("shift_hours", 0)
        prepare_from = max(window.get("prepare_from", window["start"]), now_ts) - 60
        until = window.get("until", window["end"])
        span = f"{window['from']}–{window['to']}" + (f" (±{shift} h)" if shift else "")
        for slot in _slots_between(ranges, datetime.fromtimestamp(prepare_from, tz), datetime.fromtimestamp(until, tz)):
            program = by_slot.get(slot)
            if program is None:
                continue
            notes = []
            if (program.get("soc_percent") or 0) < target:
                program["soc_percent"] = target
                notes.append(
                    f"{target}% for the outage expected {span} (~{need:.1f} kWh; it happened yesterday at about that time"
                    + (f"; strict: {strict}" if strict else "") + ")."
                )
            if not keep_grid_charge and program.get("grid_charge") is False:
                program["grid_charge"] = True
                notes.append(f"Grid charge on so the battery is full whenever the outage starts.")
            if notes:
                program["reason"] = f"{program.get('reason') or ''} {' '.join(notes)}".strip()
                if slot not in changed:
                    changed.append(slot)
    if changed:
        spans = ", ".join(f"{w['from']}–{w['to']}" for w in windows)
        shift = windows[0].get("shift_hours", 0)
        result["summary"] = (
            f"{result.get('summary') or ''} Expected outages ({spans}{f', ±{shift} h' if shift else ''}, as yesterday): "
            f"P{', P'.join(map(str, changed))} kept charged around them."
        ).strip()
    return changed


def apply_strict(result: dict[str, Any], opts: Options, strict: str | None) -> list[int]:
    """Long outages lately (strict): every program keeps at least strict_min_soc_percent, so
    there is a reserve for an outage at any time of day."""
    floor = min(opts.strict_min_soc_percent, opts.max_soc_percent)
    if not strict or floor <= opts.min_soc_percent:
        return []
    changed = []
    for program in result.get("deye_programs") or []:
        if program.get("soc_percent") is not None and program["soc_percent"] < floor:
            program["soc_percent"] = floor
            program["reason"] = f"{program.get('reason') or ''} Strict: at least {floor}% ({strict}).".strip()
            changed.append(program.get("slot"))
    if changed:
        result["summary"] = f"{result.get('summary') or ''} Strict mode ({strict}): every program at least {floor}%.".strip()
    return changed
