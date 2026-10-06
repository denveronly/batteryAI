"""Monthly bill: grid energy from a meter (e.g. a Shelly EM), split by tariff and priced
when it is recorded.

Every recording cycle reads the meter and adds the energy since the previous reading to
bill_days, per day and tariff, at the price in effect then. Changing a tariff later only
affects energy recorded after the change. The meter may be a lifetime total (Shelly) or a
daily counter: a drop in its value is a reset, and the value after it is new energy.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from collector import to_float
from config import Options
from db import Database
from ha import HAError, HomeAssistant
from history import _fetch

_LOGGER = logging.getLogger(__name__)

METER_KEY = "bill_meter"  # meta: {"entity", "value", "ts"} of the last meter reading
# Longest gap between two meter readings whose energy is still spread over the gap; after
# longer outages the energy is booked anyway, over the last MAX_SPREAD_DAYS.
MAX_SPREAD_DAYS = 31
# A small drop is meter noise, not a reset.
NOISE_KWH = 0.05

Entries = dict[tuple[str, str], tuple[float, float]]


def meter_kwh(state: dict[str, Any] | None) -> float | None:
    """The meter value in kWh (Wh and MWh are converted)."""
    if not state:
        return None
    value = to_float(state.get("state"))
    if value is None:
        return None
    unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "").strip().lower()
    return value / 1000 if unit == "wh" else value * 1000 if unit == "mwh" else value


def energy_delta(previous: float, current: float) -> float:
    """kWh used between two meter values; a drop means the meter was reset."""
    if current >= previous:
        return current - previous
    if previous - current <= NOISE_KWH:
        return 0.0
    return current


def tariff_key(opts: Options, minute_of_day: int) -> tuple[str, float]:
    """(tariff name, price) at a minute of the day."""
    price, name = opts.tariff_at(minute_of_day)
    return name, price


def split(opts: Options, tz: tzinfo, start_ts: float, end_ts: float, kwh: float, entries: Entries) -> None:
    """Spreads kWh evenly over [start_ts, end_ts) and adds it per (date, tariff) with its cost."""
    if kwh <= 0:
        return
    start_ts = max(start_ts, end_ts - MAX_SPREAD_DAYS * 86400)
    if end_ts - start_ts < 1:
        start_ts = end_ts - 1  # book it in the second it was measured
    per_second = kwh / (end_ts - start_ts)
    minute = int(start_ts) // 60 * 60
    while minute < end_ts:
        seconds = min(minute + 60, end_ts) - max(minute, start_ts)
        if seconds > 0:
            local = datetime.fromtimestamp(minute, tz)
            name, price = tariff_key(opts, local.hour * 60 + local.minute)
            key = (local.date().isoformat(), name)
            old_kwh, old_cost = entries.get(key, (0.0, 0.0))
            part = per_second * seconds
            entries[key] = (old_kwh + part, old_cost + part * price)
        minute += 60


class BillRecorder:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.backfilling = False

    @staticmethod
    def step(
        opts: Options, tz: tzinfo, entity: str, value: float, ts: float, meter: dict[str, Any] | None, entries: Entries
    ) -> dict[str, Any]:
        """Adds the energy since the previous reading to entries; returns the new meter state."""
        if meter and meter.get("entity") == entity and meter.get("value") is not None:
            if ts <= meter["ts"]:
                return meter
            split(opts, tz, meter["ts"], ts, energy_delta(meter["value"], value), entries)
            if value < meter["value"] and meter["value"] - value <= NOISE_KWH:
                value = meter["value"]  # ignore the dip, keep counting from the higher value
        return {"entity": entity, "value": value, "ts": ts}

    async def record(self, ha: HomeAssistant, opts: Options, tz: tzinfo) -> None:
        """One live reading of the meter (called every recording cycle)."""
        entity = opts.bill_sensor
        if not entity or self.backfilling:
            return
        meter = self.db.meta_get(METER_KEY)
        if not meter or meter.get("entity") != entity:
            await self.backfill(ha, opts, tz, meter["ts"] if meter else None)
            return
        value = meter_kwh(await ha.state(entity))
        if value is not None:
            entries: Entries = {}
            state = self.step(opts, tz, entity, value, datetime.now(tz).timestamp(), meter, entries)
            self.db.add_bill(entries, METER_KEY, state)

    async def backfill(self, ha: HomeAssistant, opts: Options, tz: tzinfo, since_ts: float | None) -> None:
        """First reading of a meter: take this month so far from the Home Assistant recorder
        (as far as it keeps history), then continue live. After switching to another meter,
        only the time since the old meter's last reading is taken (since_ts)."""
        entity = opts.bill_sensor
        self.backfilling = True
        try:
            now = datetime.now(tz)
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if since_ts is not None:
                start = max(start, datetime.fromtimestamp(since_ts, tz))
            unit = None
            try:
                unit = ((await ha.fetch_state(entity)).get("attributes") or {}).get("unit_of_measurement")
                points = await _fetch(ha, entity, start, now)
            except HAError as err:
                _LOGGER.warning("Monthly bill: no history for %s (%s); starting from now", entity, err)
                points = []
            meter: dict[str, Any] | None = None
            entries: Entries = {}
            booked = 0
            for ts, raw in sorted(points, key=lambda p: p[0]):
                value = meter_kwh({"state": raw, "attributes": {"unit_of_measurement": unit}})
                if value is None:
                    continue
                meter = self.step(opts, tz, entity, value, max(ts, start.timestamp()), meter, entries)
                booked += 1
            value = meter_kwh(await ha.state(entity))
            if value is not None:
                meter = self.step(opts, tz, entity, value, now.timestamp(), meter, entries)
            if meter is None:
                _LOGGER.warning("Monthly bill: %s has no value yet", entity)
                return
            self.db.add_bill(entries, METER_KEY, meter)
            _LOGGER.info("Monthly bill: started recording %s (%d values from this month's history)", entity, booked)
        finally:
            self.backfilling = False


def reprice_month(db: Database, month: str, opts: Options) -> dict[str, Any]:
    """On request only: sets one month's prices to the current tariffs and recalculates its
    costs. With a single price every day becomes one total under that tariff; otherwise
    energy recorded under a tariff name that no longer exists keeps its price."""
    rows = db.bill_rows(f"{month}-01", f"{month}-31")
    old_prices = {tariff: price for (m, tariff), price in db.bill_prices(int(month[:4])).items() if m == month}
    prices = {t.name: t.price for t in opts.tariffs}
    entries: dict[tuple[str, str], tuple[float, float]] = {}
    used: dict[str, float] = {}
    for row in rows:
        if opts.single_price and opts.tariffs:
            name = opts.tariffs[0].name
        else:
            name = row["tariff"]
        price = prices.get(name, old_prices.get(name, row["cost"] / row["kwh"] if row["kwh"] else 0.0))
        used[name] = price
        kwh = entries.get((row["local_date"], name), (0.0, 0.0))[0] + row["kwh"]
        entries[(row["local_date"], name)] = (kwh, kwh * price)
    db.replace_bill_month(month, entries, used)
    return {"month": month, "days": len({day for day, _ in entries}), "cost": round(sum(c for _, c in entries.values()), 2)}


def bill_report(db: Database, year: int, today: date, opts: Options) -> dict[str, Any]:
    """Months of one year with energy and cost per tariff, plus the year's total."""
    rows = db.bill_rows(f"{year:04d}-01-01", f"{year:04d}-12-31")
    # Most expensive tariff first (Peak before Off-peak); names no longer in the settings last.
    prices = {t.name: t.price for t in opts.tariffs}
    names = sorted({r["tariff"] for r in rows}, key=lambda n: (n not in prices, -prices.get(n, 0), n))

    def empty(key: str) -> dict[str, Any]:
        return {"key": key, "by_tariff": {}, "kwh": 0.0, "cost": 0.0}

    def add(target: dict[str, Any], row: dict[str, Any]) -> None:
        entry = target["by_tariff"].setdefault(row["tariff"], {"kwh": 0.0, "cost": 0.0})
        entry["kwh"] += row["kwh"]
        entry["cost"] += row["cost"]
        target["kwh"] += row["kwh"]
        target["cost"] += row["cost"]

    month_prices = db.bill_prices(year)
    months: dict[str, dict[str, Any]] = {}
    total = empty(str(year))
    for row in rows:
        month = months.get(row["local_date"][:7])
        if month is None:
            month = months[row["local_date"][:7]] = {**empty(row["local_date"][:7]), "days": {}}
        day = month["days"].setdefault(row["local_date"], empty(row["local_date"]))
        add(day, row)
        add(month, row)
        add(total, row)

    def rounded(item: dict[str, Any]) -> dict[str, Any]:
        item["kwh"] = round(item["kwh"], 2)
        item["cost"] = round(item["cost"], 2)
        item["by_tariff"] = {k: {"kwh": round(v["kwh"], 2), "cost": round(v["cost"], 2)} for k, v in item["by_tariff"].items()}
        return item

    out = []
    for key in sorted(months, reverse=True):
        month = rounded(months[key])
        month["prices"] = {name: round(price, 4) for (m, name), price in month_prices.items() if m == key}
        month["days"] = [rounded(d) for _, d in sorted(month["days"].items(), reverse=True)]
        month["day_count"] = len(month["days"])
        out.append(month)
    years = sorted(set(db.bill_years()) | {today.year}, reverse=True)
    return {
        "year": year,
        "years": years,
        "tariffs": names,
        "months": out,
        "total": rounded(total),
        "currency": opts.tariff_currency,
        "sensor": opts.bill_sensor,
        "current_tariffs": [t.name for t in opts.tariffs],
        "meter": db.meta_get(METER_KEY),
    }
