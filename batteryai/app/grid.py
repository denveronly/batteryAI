"""Grid availability: the grid status sensor is checked every minute, every outage is stored
(start, end, duration), and yesterday's outages are expected again today and tomorrow at the
same times (today's so far also tomorrow)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, tzinfo
from typing import Any

from collector import to_float
from config import Options
from db import Database
from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)

BACKFILL_KEY = "grid_backfill"  # meta: the entity whose history was imported
BACKFILL_DAYS = 14
MIN_OUTAGE_MINUTES = 3  # shorter blips are not planned for

DOWN_WORDS = ("off-grid", "off grid", "offgrid", "disconnected", "outage", "no grid", "lost", "fault", "down",
              "off", "false", "відсутн", "немає", "нет", "отсутств")
UP_WORDS = ("on-grid", "on grid", "ongrid", "connected", "normal", "online", "on", "true", "grid", "є", "есть")


def grid_up(state: dict[str, Any] | None) -> bool | None:
    """True when the grid has power, False in an outage, None when unknown. Works with a
    binary_sensor (on = power; device_class problem is reversed), a voltage sensor (V, above
    100 = power), a number (above 0 = power) or a status text (On-Grid / Off-Grid …)."""
    if not state:
        return None
    raw = str(state.get("state") or "").strip()
    text = raw.lower()
    if text in ("", "unknown", "unavailable", "none"):
        return None
    attributes = state.get("attributes") or {}
    entity_id = str(state.get("entity_id") or "")
    if entity_id.startswith("binary_sensor.") and text in ("on", "off"):
        up = text == "on"
        return not up if attributes.get("device_class") == "problem" else up
    number = to_float(raw)
    if number is not None:
        unit = str(attributes.get("unit_of_measurement") or "").strip().lower()
        return number > 100 if unit in ("v", "volt", "volts") else number > 0
    for word in DOWN_WORDS:
        if word in text:
            return False
    for word in UP_WORDS:
        if word in text:
            return True
    return None


class GridMonitor:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.up: bool | None = None
        self.checked: float | None = None
        self.backfilling = False

    def _record(self, up: bool, ts: float) -> None:
        open_row = self.db.open_grid_outage()
        if not up and open_row is None:
            self.db.add_grid_outage(int(ts))
            _LOGGER.info("Grid outage started at %s", datetime.fromtimestamp(ts).isoformat(timespec="minutes"))
        elif up and open_row is not None:
            self.db.end_grid_outage(open_row["id"], int(ts))
            _LOGGER.info("Grid back after %.0f minutes", (ts - open_row["start_ts"]) / 60)

    async def check(self, ha: HomeAssistant, opts: Options) -> bool | None:
        """Reads the grid status sensor and records an outage starting or ending."""
        if not opts.grid_status_sensor:
            self.up = None
            return None
        up = grid_up(await ha.state(opts.grid_status_sensor))
        self.checked = datetime.now().timestamp()
        if up is not None:
            self.up = up
            self._record(up, self.checked)
        return up

    async def backfill(self, ha: HomeAssistant, opts: Options) -> None:
        """Once per sensor: the outages of the last two weeks from the Home Assistant recorder."""
        from history import _fetch  # history imports the collector, like this module

        entity = opts.grid_status_sensor
        if not entity or self.db.meta_get(BACKFILL_KEY) == entity or self.backfilling:
            return
        self.backfilling = True
        try:
            end = datetime.now().astimezone()
            start = end - timedelta(days=BACKFILL_DAYS)
            try:
                attributes = (await ha.fetch_state(entity)).get("attributes") or {}
                points = await _fetch(ha, entity, start, end)
            except HAError as err:
                _LOGGER.warning("Grid history of %s could not be read: %s", entity, err)
                return
            first_live = self.db.first_grid_outage_ts()
            intervals, down_since = [], None
            for ts, raw in sorted(points, key=lambda p: p[0]):
                up = grid_up({"state": raw, "attributes": attributes, "entity_id": entity})
                if up is False and down_since is None:
                    down_since = ts
                elif up and down_since is not None:
                    intervals.append((int(down_since), int(ts)))
                    down_since = None
            if first_live is not None:
                intervals = [(a, b) for a, b in intervals if b <= first_live]
            self.db.add_grid_outages(intervals)
            self.db.meta_set(BACKFILL_KEY, entity)
            _LOGGER.info("Grid history: %d outages of the last %d days imported from %s", len(intervals), BACKFILL_DAYS, entity)
        finally:
            self.backfilling = False


def _fmt(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


def outages(db: Database, tz: tzinfo, since_ts: float, now_ts: float) -> list[dict[str, Any]]:
    out = []
    for row in db.grid_outages_since(int(since_ts)):
        end = row["end_ts"] or now_ts
        out.append({
            "start": row["start_ts"], "end": row["end_ts"], "ongoing": row["end_ts"] is None,
            "minutes": round((end - row["start_ts"]) / 60), "start_local": _fmt(row["start_ts"], tz),
            "end_local": _fmt(row["end_ts"], tz) if row["end_ts"] else None,
        })
    return out


def _day_windows(rows: list[dict[str, Any]], day: datetime, tz: tzinfo, now_ts: float) -> list[tuple[int, int]]:
    """Outages that touched one day, as (start minute, end minute) of that day."""
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day_end = day_start + 86400
    windows = []
    for row in rows:
        start, end = row["start"], row["end"] or now_ts
        if end <= day_start or start >= day_end or (end - start) < MIN_OUTAGE_MINUTES * 60:
            continue
        windows.append((int((max(start, day_start) - day_start) // 60), int((min(end, day_end) - day_start) // 60)))
    return windows


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def expected(db: Database, tz: tzinfo, now_ts: float) -> dict[str, Any]:
    """Expected outage windows: yesterday's outages repeat today and tomorrow at the same
    times, today's (so far) repeat tomorrow."""
    now = datetime.fromtimestamp(now_ts, tz)
    rows = outages(db, tz, now_ts - 3 * 86400, now_ts)
    yesterday = _day_windows(rows, now - timedelta(days=1), tz, now_ts)
    today_so_far = _day_windows(rows, now, tz, now_ts)

    def as_dict(windows: list[tuple[int, int]], day: datetime, source: str) -> list[dict[str, Any]]:
        base = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            {
                "start": (base + timedelta(minutes=a)).timestamp(), "end": (base + timedelta(minutes=b)).timestamp(),
                "from": f"{a // 60:02d}:{a % 60:02d}", "to": f"{b // 60:02d}:{b % 60:02d}" if b < 1440 else "24:00",
                "minutes": b - a, "source": source,
            }
            for a, b in _merge(windows)
        ]

    tomorrow = now + timedelta(days=1)
    return {
        "today": [w for w in as_dict(yesterday, now, "yesterday") if w["end"] > now_ts],
        "tomorrow": as_dict(yesterday + today_so_far, tomorrow, "yesterday and today"),
    }


def summary(db: Database, tz: tzinfo, now_ts: float) -> dict[str, Any]:
    """For the predictions: the last week's outages and the expected windows."""
    week = outages(db, tz, now_ts - 7 * 86400, now_ts)
    return {
        "outages_last_7_days": [
            {"start": o["start_local"], "end": o["end_local"] or "ongoing", "minutes": o["minutes"]} for o in week
        ],
        "outage_count_7_days": len(week),
        "outage_minutes_7_days": sum(o["minutes"] for o in week),
        "expected": {k: [{"from": w["from"], "to": w["to"]} for w in v] for k, v in expected(db, tz, now_ts).items()},
    }
