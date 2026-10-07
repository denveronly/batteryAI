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
    state.setdefault("keep_grid_charge", False)  # predictions leave the grid-charge switches alone
    state.setdefault("emergency_enabled", True)  # charge right away while emergency outages are on
    state.setdefault("precharge_smart", True)  # tariff-aware: charge only what the outage needs
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


SELECT_DOMAINS = ("select", "input_select")
ON_WORDS = {"on", "true", "yes", "enabled", "enable", "1"}
OFF_WORDS = ("disabled", "disable", "off", "no", "none", "false", "0")


def charge_is_on(state: Any) -> bool:
    """Grid charge on: a switch that is on, or a select on an option with grid charging
    (Deye "Grid", "Grid & Gen", "Enabled")."""
    text = str(state or "").strip().lower()
    return text in ON_WORDS or "grid" in text


def _charge_option(options: list[str], on: bool) -> str | None:
    lowered = [(o, o.strip().lower()) for o in options]
    if on:
        for test in (
            lambda t: t == "grid",
            lambda t: "grid" in t and "gen" not in t,
            lambda t: "grid" in t,
            lambda t: t in ON_WORDS,
        ):
            match = next((o for o, t in lowered if test(t)), None)
            if match:
                return match
        return None
    return next((o for word in OFF_WORDS for o, t in lowered if t == word), None)


async def set_switch(ha: HomeAssistant, entity_id: str, on: bool, option: str | None = None) -> dict[str, Any]:
    """Turns grid charge of a program on/off: a switch / input_boolean, or a select whose
    options name it (Deye: Disabled / Grid / …). option restores an exact select option."""
    domain = entity_id.split(".", 1)[0]
    if domain in SWITCH_DOMAINS:
        await ha.call_service(domain, "turn_on" if on else "turn_off", {"entity_id": entity_id})
        return {"entity_id": entity_id, "to": "on" if on else "off"}
    if domain in SELECT_DOMAINS:
        options = [str(o) for o in ((await ha.fetch_state(entity_id)).get("attributes") or {}).get("options") or []]
        choice = option if option in options else _charge_option(options, on)
        if choice is None:
            raise HAError("unsupported", f"{entity_id}: no option for grid charge {'on' if on else 'off'} in {options}")
        await ha.call_service(domain, "select_option", {"entity_id": entity_id, "option": choice})
        return {"entity_id": entity_id, "to": choice}
    raise HAError("unsupported", f"{entity_id}: only switch, input_boolean or select entities can turn grid charge on/off")


async def apply_prediction(
    ha: HomeAssistant, opts: Options, result: dict[str, Any], keep_grid_charge: bool = False
) -> list[dict[str, Any]]:
    """Writes the suggested program SOCs, clamped to the configured range.

    A program is only changed when the suggestion differs from the current value by at least
    the apply threshold, so small fluctuations do not rewrite the inverter every run.
    """
    by_slot = {p.get("slot"): p for p in result.get("deye_programs") or []}
    actions = []
    for program in opts.deye_programs:
        suggested = by_slot.get(program.slot) or {}
        if program.charge_entity and isinstance(suggested.get("grid_charge"), bool) and not keep_grid_charge:
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
        if charge_is_on(current) == on:
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
                if charge_is_on(previous):
                    action.update(status="unchanged", value=previous, suggested="on")
                else:
                    action.update(status="set", **{"from": previous}, **await set_switch(ha, program.charge_entity, True))
            except HAError as err:
                action.update(status="error", error=str(err))
            actions.append(action)
    return actions


async def ensure_charge(ha: HomeAssistant, opts: Options, soc: float) -> list[dict[str, Any]]:
    """While a charge is held (emergency outages): puts back any program whose SOC is below
    soc or whose grid charge is off. Returns the changes (none when all is as it should be)."""
    actions = []
    for program in opts.deye_programs:
        if program.soc_entity:
            try:
                current = to_float((await ha.fetch_state(program.soc_entity)).get("state"))
                if current is not None and current < soc - 0.5:
                    actions.append({"slot": program.slot, "entity_id": program.soc_entity, "time": time.time(),
                                    "status": "set", **await set_soc(ha, program.soc_entity, soc)})
            except HAError as err:
                actions.append({"slot": program.slot, "entity_id": program.soc_entity, "status": "error", "error": str(err)})
        if program.charge_entity:
            try:
                state = (await ha.fetch_state(program.charge_entity)).get("state")
                if not charge_is_on(state):
                    actions.append({"slot": program.slot, "entity_id": program.charge_entity, "kind": "grid_charge",
                                    "time": time.time(), "status": "set", "from": state,
                                    **await set_switch(ha, program.charge_entity, True)})
            except HAError as err:
                actions.append({"slot": program.slot, "entity_id": program.charge_entity, "kind": "grid_charge",
                                "status": "error", "error": str(err)})
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
        if previous is None or str(previous).lower() in ("unknown", "unavailable", ""):
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
            action.update(status="set", **{"from": current}, **await set_switch(ha, entity_id, charge_is_on(previous), option=previous))
        except HAError as err:
            action.update(status="error", error=str(err))
        actions.append(action)
    state["saved_switches"] = {}
    return actions
