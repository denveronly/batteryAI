"""Writes SOC targets to the Deye program entities (auto-apply, Apply button, Charge all)."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from collector import to_float
from config import DATA_DIR, Options
from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)

STATE_PATH = os.path.join(DATA_DIR, "control.json")
# off: Claude only advises. auto: predictions are written to the inverter.
# charge_all: every program is held at the charge-all SOC; predictions are not applied.
MODES = ("off", "auto", "charge_all")
SWITCH_DOMAINS = ("switch", "input_boolean")


def load_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("mode", "off")
    state.setdefault("since", None)
    state.setdefault("saved_switches", {})
    state.setdefault("precharge_enabled", True)  # charge before an outage (outage minutes sensor)
    state.setdefault("precharge", None)  # the running pre-outage charge, see BatteryAI.check_outage
    return state


def save_state(state: dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


async def set_soc(ha: HomeAssistant, entity_id: str, value: float) -> dict[str, Any]:
    """Sets an SOC entity (number, input_number or select), respecting its min/max/step."""
    state = await ha.fetch_state(entity_id)
    attributes = state.get("attributes") or {}
    current = to_float(state.get("state"))
    domain = entity_id.split(".", 1)[0]

    if domain in ("number", "input_number"):
        low = to_float(attributes.get("min")) or 0
        high = to_float(attributes.get("max")) or 100
        step = to_float(attributes.get("step")) or 1
        target = min(high, max(low, round(value / step) * step))
        target = int(target) if float(target).is_integer() else target
        await ha.call_service(domain, "set_value", {"entity_id": entity_id, "value": target})
    elif domain in ("select", "input_select"):
        options = [o for o in attributes.get("options") or [] if to_float(o) is not None]
        if not options:
            raise HAError("unsupported", f"{entity_id} has no numeric options")
        option = min(options, key=lambda o: abs(to_float(o) - value))
        target = to_float(option)
        await ha.call_service(domain, "select_option", {"entity_id": entity_id, "option": option})
    else:
        raise HAError("unsupported", f"{entity_id}: only number, input_number or select entities can be set")
    return {"entity_id": entity_id, "from": current, "to": target}


async def set_switch(ha: HomeAssistant, entity_id: str, on: bool) -> dict[str, Any]:
    domain = entity_id.split(".", 1)[0]
    if domain not in SWITCH_DOMAINS:
        raise HAError("unsupported", f"{entity_id}: only switch or input_boolean entities can be turned on/off")
    await ha.call_service(domain, "turn_on" if on else "turn_off", {"entity_id": entity_id})
    return {"entity_id": entity_id, "to": "on" if on else "off"}


async def apply_prediction(ha: HomeAssistant, opts: Options, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Writes the suggested program SOCs, clamped to the configured range.

    A program is only changed when the suggestion differs from the current value by at least
    the apply threshold, so small fluctuations do not rewrite the inverter every run.
    """
    by_slot = {p.get("slot"): p for p in result.get("deye_programs") or []}
    actions = []
    for program in opts.deye_programs:
        suggested = by_slot.get(program.slot) or {}
        if program.charge_entity and isinstance(suggested.get("grid_charge"), bool):
            actions.append(await _apply_switch(ha, program.slot, program.charge_entity, suggested["grid_charge"]))
        suggestion = suggested.get("soc_percent")
        if not program.soc_entity or suggestion is None:
            continue
        target = min(opts.max_soc_percent, max(opts.min_soc_percent, float(suggestion)))
        action: dict[str, Any] = {"slot": program.slot, "entity_id": program.soc_entity, "time": time.time()}
        try:
            current = to_float((await ha.fetch_state(program.soc_entity)).get("state"))
            if current is not None and abs(target - current) < opts.apply_threshold_percent:
                action.update(status="unchanged", value=current, suggested=target)
            else:
                action.update(status="set", **await set_soc(ha, program.soc_entity, target))
        except HAError as err:
            action.update(status="error", error=str(err))
        actions.append(action)
    _LOGGER.info("Applied prediction: %s", actions)
    return actions


async def _apply_switch(ha: HomeAssistant, slot: int, entity_id: str, on: bool) -> dict[str, Any]:
    """Grid charge of one program, only touched when it differs from what Claude wants."""
    action: dict[str, Any] = {"slot": slot, "entity_id": entity_id, "kind": "grid_charge", "time": time.time()}
    try:
        current = (await ha.fetch_state(entity_id)).get("state")
        if current == ("on" if on else "off"):
            action.update(status="unchanged", value=current, suggested="on" if on else "off")
        else:
            action.update(status="set", **{"from": current}, **await set_switch(ha, entity_id, on))
    except HAError as err:
        action.update(status="error", error=str(err))
    return action


async def charge_all(
    ha: HomeAssistant, opts: Options, state: dict[str, Any], soc: float | None = None
) -> list[dict[str, Any]]:
    """Sets every program to the charge-all SOC (or soc) and turns on its grid-charge switch, if configured."""
    actions = []
    for program in opts.deye_programs:
        if program.soc_entity:
            action: dict[str, Any] = {"slot": program.slot, "entity_id": program.soc_entity, "time": time.time()}
            try:
                action.update(status="set", **await set_soc(ha, program.soc_entity, opts.charge_all_soc_percent if soc is None else soc))
            except HAError as err:
                action.update(status="error", error=str(err))
            actions.append(action)
        if program.charge_entity:
            action = {"slot": program.slot, "entity_id": program.charge_entity, "kind": "grid_charge", "time": time.time()}
            try:
                previous = (await ha.fetch_state(program.charge_entity)).get("state")
                if program.charge_entity not in state["saved_switches"]:
                    state["saved_switches"][program.charge_entity] = {"slot": program.slot, "state": previous}
                if previous == "on":
                    action.update(status="unchanged", value="on", suggested="on")
                else:
                    action.update(status="set", **{"from": previous}, **await set_switch(ha, program.charge_entity, True))
            except HAError as err:
                action.update(status="error", error=str(err))
            actions.append(action)
    return actions


async def read_socs(ha: HomeAssistant, opts: Options) -> dict[str, float]:
    """Current SOC of every program entity, to put back after a pre-outage charge."""
    socs = {}
    for program in opts.deye_programs:
        if program.soc_entity:
            try:
                value = to_float((await ha.fetch_state(program.soc_entity)).get("state"))
            except HAError:
                continue
            if value is not None:
                socs[program.soc_entity] = value
    return socs


async def restore_socs(ha: HomeAssistant, opts: Options, socs: dict[str, float]) -> list[dict[str, Any]]:
    actions = []
    for program in opts.deye_programs:
        if program.soc_entity in socs:
            action: dict[str, Any] = {"slot": program.slot, "entity_id": program.soc_entity, "time": time.time()}
            try:
                action.update(status="set", **await set_soc(ha, program.soc_entity, socs[program.soc_entity]))
            except HAError as err:
                action.update(status="error", error=str(err))
            actions.append(action)
    return actions


async def restore_switches(ha: HomeAssistant, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Puts grid-charge switches back the way they were before Charge all."""
    actions = []
    for entity_id, saved in state["saved_switches"].items():
        previous = saved.get("state") if isinstance(saved, dict) else saved
        if previous not in ("on", "off"):
            continue
        action: dict[str, Any] = {
            "slot": saved.get("slot") if isinstance(saved, dict) else None,
            "entity_id": entity_id,
            "kind": "grid_charge",
            "time": time.time(),
        }
        try:
            current = (await ha.fetch_state(entity_id)).get("state")
            if current == previous:
                continue
            action.update(status="set", **{"from": current}, **await set_switch(ha, entity_id, previous == "on"))
        except HAError as err:
            action.update(status="error", error=str(err))
        actions.append(action)
    state["saved_switches"] = {}
    return actions
