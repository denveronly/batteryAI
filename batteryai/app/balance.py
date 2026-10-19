"""Cell Balance to 100% in morning – a fixed rule, no AI.

At 06:30, if program 1 is running and the battery is above 95%, program 1's SOC is set
to 100% (grid charge turned on if it was off), so the battery tops up to full and the BMS
can balance the cells. While program 1 runs the 100% is held (a prediction that lowers it
is put back); when program 1 ends its previous SOC and grid charge come back.

State in control.json under "balance":
  enabled  – the toggle on the dashboard
  checked  – the local date the 06:30 check last ran
  last     – what that check did (for the dashboard)
  active   – while the 100% is held: {"soc": previous SOC, "grid": previous grid-charge
             state or None, "set": the SOC actually written}
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, tzinfo
from typing import Any

import control
from collector import program_ranges, to_float
from config import Options
from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)

NAME = "Cell Balance to 100% in morning"
SLOT = 1
CHECK_AT = 6 * 60 + 30  # 06:30
CHECK_WINDOW = 10  # minutes after CHECK_AT the check may still run (add-on restarts)
SOC_ABOVE = 95.0
TARGET = 100


def state(ctl: dict[str, Any]) -> dict[str, Any]:
    return ctl.setdefault("balance", {"enabled": False, "checked": None, "last": None, "active": None})


def _program(opts: Options):
    return next((p for p in opts.deye_programs if p.slot == SLOT and p.soc_entity), None)


def _running(snapshot: dict[str, Any] | None, opts: Options, minute: int) -> bool:
    """Is program 1 in effect at this minute of the day (from the program times)?"""
    programs = [p for p in (snapshot or {}).get("deye_programs") or [] if p.get("slot") is not None]
    span = program_ranges(programs, opts.program_time_marks == "end").get(SLOT)
    if not span or span[0] == span[1]:
        return False
    start, end = span
    return start <= minute < end if start < end else minute >= start or minute < end


async def check(
    ha: HomeAssistant, opts: Options, ctl: dict[str, Any], snapshot: dict[str, Any] | None, tz: tzinfo,
) -> list[dict[str, Any]] | None:
    """Runs every minute. Returns the actions taken (to notify), or None when nothing changed."""
    st = state(ctl)
    program = _program(opts)
    now = datetime.now(tz)
    minute = now.hour * 60 + now.minute
    running = _running(snapshot, opts, minute)

    if st.get("active"):
        if program is None:
            st["active"] = None
            return None
        if running and st.get("enabled") and ctl.get("mode") != "charge_all":
            # Hold 100% while program 1 runs (a prediction may have lowered it).
            current = to_float((await ha.fetch_state(program.soc_entity)).get("state"))
            if current is not None and current < st["active"]["set"] - 0.5:
                action = {"slot": SLOT, "entity_id": program.soc_entity, "time": time.time(), "status": "set",
                          **await control.set_soc(ha, program.soc_entity, TARGET)}
                _LOGGER.info("%s: program 1 put back to %s%%", NAME, action["to"])
                return [action]
            return None
        return await _restore(ha, program, ctl, st)

    if not st.get("enabled") or program is None or st.get("checked") == now.date().isoformat():
        return None
    if not CHECK_AT <= minute < CHECK_AT + CHECK_WINDOW:
        return None
    st["checked"] = now.date().isoformat()
    soc = to_float(((await ha.state(opts.battery_soc_sensor)) or {}).get("state")) if opts.battery_soc_sensor else None
    why = None
    if ctl.get("mode") == "charge_all":
        why = "Charge all / outage charging is active"
    elif not running:
        why = "program 1 is not running at 06:30"
    elif soc is None:
        why = "the battery SOC sensor has no value"
    elif soc <= SOC_ABOVE:
        why = f"battery at {soc:g}% (needs above {SOC_ABOVE:g}%)"
    if why:
        st["last"] = {"date": st["checked"], "done": False, "soc": soc, "reason": why}
        control.save_state(ctl)
        _LOGGER.info("%s skipped: %s", NAME, why)
        return None

    previous = to_float((await ha.fetch_state(program.soc_entity)).get("state"))
    actions = []
    written = await control.set_soc(ha, program.soc_entity, TARGET)
    actions.append({"slot": SLOT, "entity_id": program.soc_entity, "time": time.time(), "status": "set", **written})
    grid_before = None
    if program.charge_entity:
        try:
            grid_before = (await ha.fetch_state(program.charge_entity)).get("state")
            if not control.charge_is_on(grid_before):
                actions.append({"slot": SLOT, "entity_id": program.charge_entity, "kind": "grid_charge",
                                "time": time.time(), "status": "set", "from": grid_before,
                                **await control.set_switch(ha, program.charge_entity, True)})
            else:
                grid_before = None  # already on: nothing to put back
        except HAError as err:
            _LOGGER.warning("%s: grid charge of program 1 not changed: %s", NAME, err)
            grid_before = None
    st["active"] = {"soc": previous, "grid": grid_before, "set": float(written["to"])}
    st["last"] = {"date": st["checked"], "done": True, "soc": soc,
                  "reason": f"battery at {soc:g}%: program 1 {previous:g}% → {written['to']}%"}
    control.save_state(ctl)
    _LOGGER.info("%s: battery %s%%, program 1 set to %s%%", NAME, soc, written["to"])
    return actions


async def _restore(ha: HomeAssistant, program, ctl: dict[str, Any], st: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Program 1 is over (or the rule was switched off): previous SOC and grid charge back,
    unless they were changed by something else in the meantime."""
    active, actions = st["active"], []
    try:
        current = to_float((await ha.fetch_state(program.soc_entity)).get("state"))
        if active.get("soc") is not None and current is not None and abs(current - active["set"]) < 0.5:
            actions.append({"slot": SLOT, "entity_id": program.soc_entity, "time": time.time(), "status": "set",
                            **await control.set_soc(ha, program.soc_entity, active["soc"])})
        if active.get("grid") is not None and program.charge_entity:
            now_state = (await ha.fetch_state(program.charge_entity)).get("state")
            if control.charge_is_on(now_state):
                actions.append({"slot": SLOT, "entity_id": program.charge_entity, "kind": "grid_charge",
                                "time": time.time(), "status": "set", "from": now_state,
                                **await control.set_switch(ha, program.charge_entity, False)})
    except HAError as err:
        _LOGGER.warning("%s: restoring program 1 failed: %s", NAME, err)
        return None
    st["active"] = None
    control.save_state(ctl)
    _LOGGER.info("%s over: program 1 restored (%d changes)", NAME, len(actions))
    return actions or None
