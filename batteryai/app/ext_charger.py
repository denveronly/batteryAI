"""External charger – a fixed rule, no AI.

A separate charger (a switch in Home Assistant) charges the battery in the cheapest
(night) tariff:
  * when the night tariff starts, it is turned on if the battery is below start_below %;
  * it is turned off when the battery reaches stop_at %, or when the night tariff ends;
  * while the grid draw is above grid_limit kW it is paused (off) – it comes back on when the
    grid draw has been below grid_resume kW (and at least PAUSE_MIN minutes have passed);
  * at or above stop_at % it is always turned off (also when switched on by hand);
  * while the grid draw without the charger is below grid_low kW and the inverters do not
    charge the battery (an outage, or no grid charging), it is turned off;
  * when the inverters start charging the battery (battery power sensor) and it is below
    start_below %, it is turned on – at any time of day – until stop_at %;
  * turned off by hand, it stays off until the next night (or the next day).

State in control.json under "ext_charger":
  enabled   – the toggle on the dashboard
  window    – start of the night window being handled (timestamp)
  decided   – the start check of this window ran
  session   – charging is wanted in this window
  paused    – off because of the grid limit (since paused_at)
  on_by_us  – BatteryAI turned the switch on
  last      – the last change {ts, text}
  live      – the last readings for the dashboard
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, tzinfo
from typing import Any

from collector import to_float
from config import Options
from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)

NAME = "External charger"
PAUSE_MIN = 5  # minutes off at least after the grid limit was hit (no on/off flapping)
SWITCH_DOMAINS = ("switch", "input_boolean", "light", "fan")


def state(ctl: dict[str, Any]) -> dict[str, Any]:
    return ctl.setdefault("ext_charger", {"enabled": False, "window": None, "decided": False, "session": False,
                                          "paused": False, "on_by_us": False, "last": None, "live": {}})


def window_start(opts: Options, now: datetime) -> datetime | None:
    """Start of the cheapest-tariff window we are in (None outside it)."""
    minute = now.hour * 60 + now.minute
    if not opts.is_cheap(minute):
        return None
    start = now.replace(second=0, microsecond=0)
    for _ in range(1440):
        before = start - timedelta(minutes=1)
        if not opts.is_cheap(before.hour * 60 + before.minute):
            return start
        start = before
    return start  # cheap all day (one price): the day's start is as good as any


def next_window(opts: Options, now: datetime) -> datetime | None:
    t = now.replace(second=0, microsecond=0)
    for _ in range(2 * 1440):
        t += timedelta(minutes=1)
        if opts.is_cheap(t.hour * 60 + t.minute) and not opts.is_cheap((t - timedelta(minutes=1)).hour * 60 + (t - timedelta(minutes=1)).minute):
            return t
    return None


async def _value(ha: HomeAssistant, entity_id: str) -> tuple[float | None, str]:
    if not entity_id:
        return None, ""
    s = await ha.state(entity_id)
    if not s:
        return None, ""
    return to_float(s.get("state")), str((s.get("attributes") or {}).get("unit_of_measurement") or "")


def _kw(value: float | None, unit: str) -> float | None:
    """kW from a W or kW sensor (no unit: above 200 is taken as W)."""
    if value is None:
        return None
    u = unit.strip().lower()
    if u == "kw":
        return value
    if u == "w" or (not u and abs(value) > 200):
        return value / 1000
    return value


async def _switch(ha: HomeAssistant, entity_id: str, on: bool) -> None:
    domain = entity_id.split(".", 1)[0]
    await ha.call_service(domain, "turn_on" if on else "turn_off", {"entity_id": entity_id})


async def check(ha: HomeAssistant, opts: Options, ctl: dict[str, Any], tz: tzinfo) -> list[str]:
    """Runs every minute. Returns notification lines for what was switched.

    Order of the rules:
      1. on at or above stop_at % (also when switched on by hand) → off;
      2. grid draw above the limit → off, paused (back on below grid_resume after PAUSE_MIN);
      3. grid draw without the charger below grid_low while the inverters do not charge the
         battery (outage, or no grid charging) → off;
      4. night tariff starts and the battery is below start_below → on (until stop_at or the
         night ends);
      5. the inverters charge the battery and it is below start_below → on (until stop_at).
    """
    st = state(ctl)
    entity = opts.ext_charger_switch
    if not entity:
        return []
    now = datetime.now(tz)
    t = time.time()
    soc, _ = await _value(ha, opts.ext_charger_soc_sensor or opts.battery_soc_sensor)
    grid_raw, grid_unit = await _value(ha, opts.ext_charger_grid_sensor)
    grid_kw = _kw(grid_raw, grid_unit)
    power_raw, power_unit = await _value(ha, opts.ext_charger_power_sensor)
    charger_kw = _kw(power_raw, power_unit)
    bat_raw, bat_unit = await _value(ha, opts.ext_charger_battery_power_sensor)
    bat_kw = _kw(bat_raw, bat_unit)
    # Inverters charging the battery: its power in the charging direction above 1 kW.
    inverters_charging = None if bat_kw is None else (-bat_kw if opts.ext_charger_charge_negative else bat_kw) > 1.0
    switch_state = await ha.state(entity)
    is_on = str((switch_state or {}).get("state")).lower() == "on"
    # The grid draw without the charger itself (its own power would keep it above the low limit).
    grid_net = None if grid_kw is None else max(0.0, grid_kw - (charger_kw if is_on and charger_kw is not None else 0))
    start = window_start(opts, now)
    nxt = None if start else next_window(opts, now)
    st["live"] = {"soc": soc, "grid_kw": grid_kw, "grid_net_kw": grid_net, "charger_kw": charger_kw, "on": is_on,
                  "battery_kw": bat_kw, "inverters_charging": inverters_charging, "ts": t,
                  "in_window": start is not None, "next_window": nxt.timestamp() if nxt else None}
    lines: list[str] = []

    async def turn(on: bool, text: str) -> None:
        try:
            await _switch(ha, entity, on)
        except HAError as err:
            _LOGGER.warning("%s: %s failed: %s", NAME, "on" if on else "off", err)
            st["last"] = {"ts": time.time(), "text": f"could not turn {'on' if on else 'off'}: {err}"}
            return
        st["on_by_us"] = on
        st["live"]["on"] = on
        if on:
            st["on_at"] = time.time()
        st["last"] = {"ts": time.time(), "text": text}
        lines.append(f"{'⚡ on' if on else '⏹ off'} – {text}")
        _LOGGER.info("%s %s: %s", NAME, "on" if on else "off", text)

    if not st.get("enabled"):
        if st.get("on_by_us") and is_on:
            await turn(False, "the rule was switched off")
        st["on_by_us"] = False
        return lines

    # 1. Never above the stop level – also when it was switched on by hand.
    if is_on and soc is not None and soc >= opts.ext_charger_stop_at:
        await turn(False, f"battery {soc:g}% – at or above {opts.ext_charger_stop_at}%")
        st.update(session=None, paused=False, on_by_us=False)
        return lines
    # 2. Grid draw above the limit: pause.
    if grid_kw is not None and grid_kw > opts.ext_charger_grid_limit_kw:
        if is_on:
            await turn(False, f"grid {grid_kw:.1f} kW above {opts.ext_charger_grid_limit_kw:g} kW – paused")
        if st.get("session"):
            st.update(paused=True, paused_at=t, on_by_us=False)
        return lines
    # 3. Little grid draw and the inverters not charging: outage or no grid charging.
    low = opts.ext_charger_grid_low_kw
    settled = t - (st.get("on_at") or 0) >= PAUSE_MIN * 60  # readings caught up after switching on
    if (is_on and low > 0 and grid_net is not None and grid_net < low and settled and not inverters_charging):
        await turn(False, f"grid {grid_net:.1f} kW (without the charger) below {low:g} kW"
                          + ("" if inverters_charging is None else " and the inverters are not charging"))
        st.update(session=None, paused=False, on_by_us=False)
        return lines

    if start is None:  # outside the night tariff: a night session ends
        if st.get("session") == "night":
            if st.get("on_by_us") and is_on:
                await turn(False, "the night tariff is over")
            st.update(session=None, paused=False, on_by_us=False)
        st.update(window=None, decided=False)
    else:
        key = start.timestamp()
        if st.get("window") != key:
            st.update(window=key, decided=False)
        # 4. Night tariff starts.
        if not st.get("decided"):
            if soc is None:
                st["last"] = {"ts": t, "text": "waiting for the battery SOC"}
                return lines
            st["decided"] = True
            if soc < opts.ext_charger_start_below:
                st.update(session="night", paused=False)
                if is_on:
                    return lines
                await turn(True, f"night tariff, battery {soc:g}% below {opts.ext_charger_start_below}%")
                return lines
            if soc >= opts.ext_charger_start_below:
                st["last"] = {"ts": t, "text": f"night tariff: not needed, battery {soc:g}% (start below {opts.ext_charger_start_below}%)"}

    # Paused by the grid limit: back on below the resume level.
    if st.get("session") and st.get("paused"):
        if t - (st.get("paused_at") or 0) >= PAUSE_MIN * 60 and (grid_kw is None or grid_kw < opts.ext_charger_grid_resume_kw):
            st["paused"] = False
            await turn(True, f"grid {'unknown' if grid_kw is None else f'{grid_kw:.1f} kW'} below {opts.ext_charger_grid_resume_kw:g} kW – resumed")
        return lines

    # Turned off by hand during a session: leave it off (a night session until the next night).
    if st.get("session") and st.get("on_by_us") and not is_on and t - ((st.get("last") or {}).get("ts") or 0) > 120:
        st.update(session=None, on_by_us=False, manual_off=st.get("window") or now.date().isoformat())
        st["last"] = {"ts": t, "text": "switched off by hand – left off"}
        lines.append("⏹ off – switched off by hand, left off")
        return lines

    # 5. The inverters charge the battery: on below the start level.
    if (not is_on and inverters_charging and soc is not None and soc < opts.ext_charger_start_below
            and not st.get("paused") and st.get("manual_off") != (st.get("window") or now.date().isoformat())):
        st.update(session=st.get("session") or "follow")
        await turn(True, f"the inverters are charging the battery ({abs(bat_kw):.1f} kW), battery {soc:g}% below {opts.ext_charger_start_below}%")
    return lines
