"""How much to charge before an outage when there are several tariffs (tariff-aware mode).

- Outage starting outside the cheapest tariff (peak): grid energy is expensive now, so the
  battery is only charged when it cannot carry the load from now until the outage ends, and
  then only to what the outage needs. After the outage it is topped up in the cheap hours.
- Outage starting in the cheapest tariff (off-peak): energy is cheap now, so the battery is
  charged for the whole outage; when the outage runs past the cheap hours it also covers the
  time until the next cheap hours start (the cheap window is lost to the outage).

The load is the recorded average power per hour of day (weekday or weekend), plus the
safety margin. PV is left out on purpose: it may be low during the outage.
"""

from __future__ import annotations

import re
from datetime import datetime, tzinfo
from typing import Any

from collector import is_on, to_float  # noqa: F401  (is_on: used by main)
from config import Options

STEP = 15 * 60  # seconds per integration step
DEFAULT_LOAD_W = 1000.0  # until there is history


def duration_minutes(state: dict[str, Any] | None) -> float | None:
    """Outage length in minutes: a number with its unit (min, h, s) or "H:MM[:SS]".
    A number without a unit is taken as hours up to 24, minutes above."""
    if not state:
        return None
    raw = str(state.get("state") or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if match:
        return int(match[1]) * 60 + int(match[2]) + int(match[3] or 0) / 60
    value = to_float(raw)
    if value is None or value < 0:
        return None
    unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "").strip().lower()
    if unit in ("h", "hr", "hrs", "hour", "hours", "год"):
        return value * 60
    if unit in ("s", "sec", "seconds"):
        return value / 60
    if unit in ("min", "mins", "minutes", "хв"):
        return value
    return value * 60 if value <= 24 else value


def load_profile(profile: list[dict[str, Any]]) -> dict[tuple[int, int], float]:
    """(is_weekend, hour) -> average load W from db.hourly_profile."""
    return {(p["is_weekend"], p["hour"]): p["load_power"] for p in profile if p.get("load_power") is not None}


def _load_w(loads: dict[tuple[int, int], float], local: datetime, opts: Options) -> float:
    weekend = int(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][local.weekday()] in opts.weekend_days)
    value = loads.get((weekend, local.hour), loads.get((1 - weekend, local.hour)))
    if value is None:
        value = sum(loads.values()) / len(loads) if loads else DEFAULT_LOAD_W
    return value


def energy_kwh(loads: dict[tuple[int, int], float], opts: Options, tz: tzinfo, start: float, end: float) -> float:
    total, ts = 0.0, start
    while ts < end:
        step = min(STEP, end - ts)
        total += _load_w(loads, datetime.fromtimestamp(ts, tz), opts) * step / 3_600_000
        ts += step
    return total


def _minute(opts: Options, tz: tzinfo, ts: float) -> int:
    local = datetime.fromtimestamp(ts, tz)
    return local.hour * 60 + local.minute


def next_cheap(opts: Options, tz: tzinfo, ts: float) -> float:
    """The first moment at or after ts in the cheapest tariff (within two days)."""
    t = ts
    while t < ts + 2 * 86400:
        if opts.is_cheap(_minute(opts, tz, t)):
            return t
        t += 60
    return ts


def plan(
    opts: Options, tz: tzinfo, now: float, outage_at: float, duration_min: float,
    loads: dict[tuple[int, int], float], soc: float | None,
) -> dict[str, Any]:
    """Whether to charge before this outage and to which SOC, with the reasoning."""
    end = outage_at + duration_min * 60
    margin = 1 + opts.prediction_margin_percent / 100
    capacity = opts.battery_capacity_kwh
    cheap = opts.is_cheap(_minute(opts, tz, outage_at))
    tariff = opts.tariff_at(_minute(opts, tz, outage_at))[1]
    cover_until = end
    if cheap and not opts.is_cheap(_minute(opts, tz, end)):
        cover_until = next_cheap(opts, tz, end)  # the cheap hours are lost to the outage
    need_kwh = energy_kwh(loads, opts, tz, outage_at, cover_until) * margin
    target = min(opts.outage_precharge_soc_percent, opts.min_soc_percent + need_kwh / capacity * 100)
    target = max(opts.min_soc_percent, round(target))
    available = None if soc is None else max(0.0, (soc - opts.min_soc_percent) / 100 * capacity)
    fmt = lambda t: datetime.fromtimestamp(t, tz).strftime("%H:%M")  # noqa: E731
    result: dict[str, Any] = {
        "outage_at": outage_at, "outage_end": end, "duration_min": round(duration_min),
        "tariff": tariff, "cheap": cheap, "cover_until": cover_until,
        "need_kwh": round(need_kwh, 1), "target_soc": target, "soc": soc,
    }
    if cheap:
        result["charge"] = soc is None or soc < target
        span = f"the outage {fmt(outage_at)}–{fmt(end)}" + (
            f" and until the cheap hours return at {fmt(cover_until)}" if cover_until > end else ""
        )
        result["reason"] = (
            f"Outage in {tariff} (cheapest): {span} needs ~{need_kwh:.1f} kWh → {target}%."
            + ("" if result["charge"] else f" The battery already has {soc:.0f}%.")
        )
    else:
        # Expensive now: only charge if the battery cannot carry the load until the outage ends.
        until_end = energy_kwh(loads, opts, tz, now, end) * margin
        result["charge"] = available is None or available < until_end
        if result["charge"] and soc is not None and soc > target:
            # The outage alone fits, but not with the hours before it: hold the battery.
            result["target_soc"] = target = min(opts.outage_precharge_soc_percent, int(-(-soc // 1)))
        result["reason"] = (
            f"Outage in {tariff} (not the cheapest): from now until it ends at {fmt(end)} needs "
            f"~{until_end:.1f} kWh, the battery has ~{(available or 0):.1f} kWh above the minimum. "
            + (f"Charging to {target}% for the outage itself (~{need_kwh:.1f} kWh); the rest waits for the cheap hours."
               if result["charge"] else "No charging at the expensive price; the battery covers it.")
        )
    return result
