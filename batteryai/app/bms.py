"""Battery health: live BMS values found in Home Assistant by each battery's entity prefix.

For a prefix such as "bms0" it reads, from one /api/states call:
  sensor.bms0_battery_power / _battery_current / _battery_voltage
  sensor.bms0_battery_capacity_state_of_charge        (SOC)
  sensor.bms0_cell_delta_voltage, sensor.bms0_errors_bitmask, sensor.bms0_charging_cycles,
  sensor.bms0_cell_count_real (a change from the first value seen is an alarm),
  sensor.bms0_battery_total_alarms_active
  sensor.bms0_cell_voltage_NN, sensor.bms0_cell_resistance_NN   (any number of cells)
  sensor.bms0_*temperature*                            (temperature sensors, if any)
  binary_sensor.bms0_alarm_*                           (alarms: on = active)
  binary_sensor.bms0_status_*                          (status, e.g. status_online)
"""

from __future__ import annotations

import re
from typing import Any

from collector import UNAVAILABLE, to_float
from config import Options

MAIN = {
    "power": "battery_power",
    "current": "battery_current",
    "voltage": "battery_voltage",
    "soc": "battery_capacity_state_of_charge",
    "delta": "cell_delta_voltage",
    "errors": "errors_bitmask",
    "cycles": "charging_cycles",
    "cell_count": "cell_count_real",
    "alarms_total": "battery_total_alarms_active",
}
# Readable names for the JK BMS alarm codes (binary_sensor.<prefix>_alarm_<code>).
ALARM_NAMES = {
    "batovp": "Battery over-voltage", "batuvp": "Battery under-voltage",
    "cellovp": "Cell over-voltage", "celluvp": "Cell under-voltage",
    "cellquantity": "Cell count mismatch", "wireres": "Balance wire resistance too high",
    "chocp": "Charge over-current", "dchocp": "Discharge over-current",
    "chscp": "Charge short circuit", "dchscp": "Discharge short circuit",
    "chotp": "Charge over-temperature", "chutp": "Charge under-temperature",
    "batteryovertemp": "Battery over-temperature", "mosotp": "MOS over-temperature",
    "chargemos": "Charge MOS fault", "dischargemos": "Discharge MOS fault",
    "dischargeonfailed": "Discharge switch-on failed",
    "mostempsensorabsent": "MOS temperature sensor missing",
    "temperaturesensoranomaly": "Temperature sensor anomaly",
    "cursensorerr": "Current sensor error", "cpuauxcommuerr": "CPU / aux communication error",
    "plcmoduleanomaly": "PLC module anomaly", "gpsdisconneted": "GPS disconnected",
    "modifypwdintime": "Change the password",
}
TEMP_ABSENT_RE = re.compile(r"^battempsensor(\d+)absent$")


def alarm_name(key: str, friendly: str | None) -> str:
    if key in ALARM_NAMES:
        return ALARM_NAMES[key]
    match = TEMP_ABSENT_RE.match(key)
    if match:
        return f"Battery temperature sensor {match[1]} missing"
    return friendly or _title(key)


WIRE_RE = re.compile(r"^(?:cell_)?(?:balanc\w*_)?wire_resistance_(\d{1,3})$")
CELL_RE = re.compile(r"^cell_(voltage|resistance)_(\d{1,3})$")


def _value(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("state")
    attrs = state.get("attributes") or {}
    return {
        "entity_id": state.get("entity_id"),
        "value": to_float(raw),
        "unit": attrs.get("unit_of_measurement") or "",
        "available": str(raw).strip().lower() not in UNAVAILABLE,
    }


def _title(rest: str) -> str:
    return rest.replace("_", " ").strip().capitalize()


def pack(prefix: str, states: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the BMS with this prefix reports."""
    main: dict[str, Any] = {}
    cells: dict[int, dict[str, Any]] = {}
    temps: list[dict[str, Any]] = []
    alarms: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    online = None
    sensor_start, alarm_start = f"sensor.{prefix}_", f"binary_sensor.{prefix}_alarm_"
    status_start = f"binary_sensor.{prefix}_status_"
    for state in states:
        entity = str(state.get("entity_id") or "")
        if entity.startswith(status_start):
            raw = str(state.get("state") or "").strip().lower()
            key = entity[len(status_start):]
            if key == "online":
                online = None if raw in UNAVAILABLE else raw == "on"
            else:
                statuses.append({
                    "entity_id": entity, "key": key, "on": raw == "on", "available": raw not in UNAVAILABLE,
                    "name": _title(key),
                })
            continue
        if entity.startswith(alarm_start):
            raw = str(state.get("state") or "").strip().lower()
            alarms.append({
                "entity_id": entity,
                "name": alarm_name(entity[len(alarm_start):], (state.get("attributes") or {}).get("friendly_name")),
                "key": entity[len(alarm_start):],
                "active": raw == "on",
                "available": raw not in UNAVAILABLE,
            })
            continue
        if not entity.startswith(sensor_start):
            continue
        rest = entity[len(sensor_start):]
        key = next((k for k, suffix in MAIN.items() if rest == suffix), None)
        if key:
            main[key] = _value(state)
            continue
        match = CELL_RE.match(rest)
        if match:
            cells.setdefault(int(match[2]), {"n": int(match[2])})[match[1]] = _value(state)
            continue
        match = WIRE_RE.match(rest)
        if match:  # a separate balance-wire resistance per cell, when the BMS reports one
            cells.setdefault(int(match[1]), {"n": int(match[1])})["wire"] = _value(state)
            continue
        if ("temperature" in rest or rest.startswith("temp")) and "protection" not in rest and "recovery" not in rest:
            # Names from the entity id (friendly names carry the bridge's long device name).
            nice = {"temperature_powertube": "Power tube (MOS)", "mos_temperature": "MOS"}.get(rest)
            if nice is None and rest.startswith("temperature_sensor_"):
                nice = f"Sensor {rest.rsplit('_', 1)[-1]}"
            temps.append({**_value(state), "name": nice or _title(rest)})
    cell_list = [cells[n] for n in sorted(cells)]
    volts = [c["voltage"]["value"] for c in cell_list if c.get("voltage") and c["voltage"]["value"] is not None]
    summary = {
        "cells": len(cell_list),
        "min_v": min(volts) if volts else None,
        "max_v": max(volts) if volts else None,
        "avg_v": round(sum(volts) / len(volts), 4) if volts else None,
    }
    if volts:
        summary["min_cell"] = next(c["n"] for c in cell_list if c.get("voltage", {}).get("value") == summary["min_v"])
        summary["max_cell"] = next(c["n"] for c in cell_list if c.get("voltage", {}).get("value") == summary["max_v"])
    alarms.sort(key=lambda a: (not a["active"], a["name"]))
    return {
        "main": main, "cells": cell_list, "temperatures": sorted(temps, key=lambda t: t["entity_id"]),
        "alarms": alarms, "summary": summary, "online": online,
        "statuses": sorted(statuses, key=lambda x: x["name"]),
        "active_alarms": sum(1 for a in alarms if a["active"]),
        "found": bool(main or cell_list or alarms or online is not None),
    }


def report(opts: Options, states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": b.name, "prefix": b.prefix, **pack(b.prefix, states)} for b in opts.batteries]


# Abnormal data (LiFePO4 limits): logged when it starts and when it ends.
CELL_LOW_V = 2.90
CELL_HIGH_V = 3.62
DELTA_HIGH_V = 0.050
RESISTANCE_FACTOR = 2.0  # a cell above 2× the pack's median resistance
TEMP_HIGH = 50.0       # cells / battery
TEMP_HIGH_MOS = 70.0   # power tube (MOS) runs hotter
TEMP_LOW = 0.0
CELL_RANGE_V = 0.200   # highest − lowest cell of a pack above this: serious (notified)
TEMP_SPREAD = 8.0      # battery temperature sensors of one pack further apart than this
SOC_DRIFT = 15.0       # a pack's SOC this far from the other packs' median
VOLT_DRIFT = 0.5       # a pack's voltage this far from the other packs' median
NOTIFY_ISSUES = ("cell_range",)  # abnormal data that also goes to the phone


def _volts(v: dict[str, Any] | None) -> float | None:
    if not v or v.get("value") is None:
        return None
    return v["value"] / 1000 if str(v.get("unit")).lower() == "mv" else v["value"]


def _median(values: list[float]) -> float | None:
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def issues(b: dict[str, Any], packs: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Abnormal readings now: {code: description}. With all packs, also a pack whose SOC or
    voltage drifts away from the others."""
    found: dict[str, str] = {}
    volts = [_volts(c.get("voltage")) for c in b.get("cells") or []]
    volts = [v for v in volts if v is not None]
    if len(volts) >= 2 and max(volts) - min(volts) > CELL_RANGE_V:
        found["cell_range"] = (f"Cells {min(volts):.3f}–{max(volts):.3f} V apart by {(max(volts) - min(volts)) * 1000:.0f} mV "
                               f"(above {CELL_RANGE_V * 1000:.0f} mV)")
    battery_temps = [t for t in b.get("temperatures") or [] if t.get("value") is not None
                     and "powertube" not in str(t["entity_id"]) and "mos" not in str(t["entity_id"])]
    if len(battery_temps) >= 2:
        hot = max(battery_temps, key=lambda t: t["value"])
        cold = min(battery_temps, key=lambda t: t["value"])
        if hot["value"] - cold["value"] > TEMP_SPREAD:
            found["temp_spread"] = (f"Temperatures {hot['value'] - cold['value']:.1f} °C apart ({cold['name']} {cold['value']:g}, "
                                    f"{hot['name']} {hot['value']:g}; above {TEMP_SPREAD:g} °C)")
    others = [o for o in packs or [] if o is not b and o.get("found")]
    if others:
        soc = ((b.get("main") or {}).get("soc") or {}).get("value")
        median = _median([v for o in others if (v := ((o.get("main") or {}).get("soc") or {}).get("value")) is not None])
        if soc is not None and median is not None and abs(soc - median) > SOC_DRIFT:
            found["soc_drift"] = f"SOC {soc:g} % while the other batteries are at {median:g} % (more than {SOC_DRIFT:g} % apart)"
        volt = ((b.get("main") or {}).get("voltage") or {}).get("value")
        median_v = _median([v for o in others if (v := ((o.get("main") or {}).get("voltage") or {}).get("value")) is not None])
        if volt is not None and median_v is not None and abs(volt - median_v) > VOLT_DRIFT:
            found["voltage_drift"] = f"Voltage {volt:.2f} V while the other batteries are at {median_v:.2f} V (more than {VOLT_DRIFT:g} V apart)"
    main = b.get("main") or {}
    if b.get("found") and main and all(not v.get("available") for v in main.values()):
        found["unavailable"] = "BMS values unavailable in Home Assistant"
    delta = _volts(main.get("delta"))
    if delta is not None and delta > DELTA_HIGH_V:
        found["delta_high"] = f"Cell delta {delta * 1000:.0f} mV (above {DELTA_HIGH_V * 1000:.0f} mV)"
    resistances = sorted(c["resistance"]["value"] for c in b.get("cells") or [] if c.get("resistance") and c["resistance"]["value"])
    median = resistances[len(resistances) // 2] if resistances else None
    for cell in b.get("cells") or []:
        v = _volts(cell.get("voltage"))
        if v is not None and v < CELL_LOW_V:
            found[f"cell_low_{cell['n']:02d}"] = f"Cell #{cell['n']:02d} at {v:.3f} V (below {CELL_LOW_V:.2f} V)"
        elif v is not None and v > CELL_HIGH_V:
            found[f"cell_high_{cell['n']:02d}"] = f"Cell #{cell['n']:02d} at {v:.3f} V (above {CELL_HIGH_V:.2f} V)"
        r = (cell.get("resistance") or {}).get("value")
        if r and median and len(resistances) >= 4 and r > median * RESISTANCE_FACTOR:
            unit = cell["resistance"].get("unit") or ""
            found[f"res_high_{cell['n']:02d}"] = f"Cell #{cell['n']:02d} resistance {r:g} {unit} ({r / median:.1f}× the pack's median)"
    for t in b.get("temperatures") or []:
        value = t.get("value")
        if value is None:
            continue
        key = str(t["entity_id"]).split(".", 1)[-1]
        limit = TEMP_HIGH_MOS if ("powertube" in key or "mos" in key) else TEMP_HIGH
        if value > limit:
            found[f"temp_high_{key}"] = f"{t['name']} {value:g} {t.get('unit') or '°C'} (above {limit:g})"
        elif value < TEMP_LOW:
            found[f"temp_low_{key}"] = f"{t['name']} {value:g} {t.get('unit') or '°C'} (below {TEMP_LOW:g})"
    return found


def changes(
    previous: dict[str, Any], packs: list[dict[str, Any]], now: float,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Compares alarms, online state and abnormal data with the last check. Returns the new
    state (to keep), notification lines (alarms and online changes) and log events (all
    changes). A battery seen for the first time only reports what is already wrong."""
    state: dict[str, Any] = {}
    lines: list[str] = []
    events: list[dict[str, Any]] = []
    for b in packs:
        if not b.get("found"):
            continue
        before = previous.get(b["prefix"])
        active = {a["key"]: a["name"] for a in b["alarms"] if a["active"]}
        problems = issues(b, packs)
        state[b["prefix"]] = {"alarms": sorted(active), "online": b.get("online"), "issues": problems}
        label = f"{b['name']} ({b['prefix']})"

        def event(level: str, code: str | None, message: str, notify_line: bool) -> None:
            events.append({"ts": now, "prefix": b["prefix"], "battery": b["name"], "level": level, "code": code, "message": message})
            if notify_line:
                lines.append(f"{'⚠' if level != 'ok' else '✓'} {label}: {message}" + (f" – code {code}" if code else ""))

        was = set((before or {}).get("alarms") or [])
        for key in sorted(set(active) - was):
            event("alarm", key, f"Alarm: {active[key]}", True)
        for key in sorted(was - set(active)):
            event("ok", key, f"Alarm cleared: {alarm_name(key, None)}", True)
        if before is not None:
            if before.get("online") is True and b.get("online") is False:
                event("alarm", "offline", "BMS offline", True)
            elif before.get("online") is False and b.get("online") is True:
                event("ok", "online", "BMS back online", True)
        errors = ((b.get("main") or {}).get("errors") or {}).get("value")
        old_errors = (before or {}).get("errors")
        state[b["prefix"]]["errors"] = errors
        if errors is not None and errors != old_errors and (before is not None or errors):
            if errors:
                event("alarm", f"0x{int(errors):04X}", f"BMS error bitmask {int(errors)} (0x{int(errors):04X}, bits {', '.join(str(i) for i in range(32) if int(errors) >> i & 1)})", True)
            elif old_errors:
                event("ok", "0x0000", "BMS error bitmask back to 0", True)
        # Cell count: the first value seen is the expected one; any other value is an alarm.
        count = ((b.get("main") or {}).get("cell_count") or {}).get("value")
        expected = (before or {}).get("cell_count_expected") or count
        state[b["prefix"]]["cell_count_expected"] = expected
        state[b["prefix"]]["cell_count"] = count
        old_count = (before or {}).get("cell_count")
        if count is not None and expected is not None:
            wrong, was_wrong = count != expected, old_count is not None and old_count != expected
            if wrong and (not was_wrong or count != old_count):
                event("alarm", "cell_count", f"Cell count {count:g} (expected {expected:g})", True)
            elif was_wrong and not wrong:
                event("ok", "cell_count", f"Cell count back to {expected:g}", True)
        # The BMS's own count of active alarms: notify when it rises without an alarm sensor
        # explaining it (no binary alarm turned on in this check).
        total = ((b.get("main") or {}).get("alarms_total") or {}).get("value")
        old_total = (before or {}).get("alarms_total")
        state[b["prefix"]]["alarms_total"] = total
        if total is not None and old_total is not None and total != old_total:
            new_binary = bool(set(active) - was)
            if total > old_total:
                event("alarm", "alarms_total", f"BMS reports {total:g} active alarm{'s' if total != 1 else ''} (was {old_total:g})", not new_binary)
            elif total == 0:
                event("ok", "alarms_total", "BMS reports no active alarms", False)
        old_issues = (before or {}).get("issues") or {}
        if isinstance(old_issues, list):  # older state kept only the codes
            old_issues = {k: k.replace("_", " ") for k in old_issues}
        for key in sorted(set(problems) - set(old_issues)):
            event("warning", key, problems[key], key in NOTIFY_ISSUES)
        for key in sorted(set(old_issues) - set(problems)):
            event("ok", key, f"Back to normal (was: {old_issues[key]})", key in NOTIFY_ISSUES)
    return state, lines, events


# Control: the BMS's own switches and settings (switch.<prefix>_*, number.<prefix>_*).
GROUPS = (
    ("Balancing", ("balanc",)),
    ("Cell voltages", ("cell_",)),
    ("Current", ("current",)),
    ("Temperature", ("temperature",)),
    ("Delays & times", ("delay", "_time")),
)


def group_of(key: str) -> str:
    return next((name for name, words in GROUPS if any(w in key for w in words)), "Other")


def controls(prefix: str, states: list[dict[str, Any]]) -> dict[str, Any]:
    switches, numbers = [], []
    for state in states:
        entity = str(state.get("entity_id") or "")
        attrs = state.get("attributes") or {}
        for domain, target in (("switch", switches), ("number", numbers)):
            start = f"{domain}.{prefix}_"
            if not entity.startswith(start):
                continue
            key = entity[len(start):]
            raw = state.get("state")
            item = {
                "entity_id": entity, "key": key, "group": group_of(key),
                "name": _title(key),
                "available": str(raw).strip().lower() not in UNAVAILABLE,
            }
            if domain == "switch":
                item["on"] = str(raw).strip().lower() == "on"
            else:
                item.update(value=to_float(raw), unit=attrs.get("unit_of_measurement") or "",
                            min=to_float(attrs.get("min")), max=to_float(attrs.get("max")), step=to_float(attrs.get("step")))
            target.append(item)
    order = [g for g, _ in GROUPS] + ["Other"]
    numbers.sort(key=lambda n: (order.index(n["group"]), n["key"]))
    switches.sort(key=lambda x: x["key"])
    return {"prefix": prefix, "switches": switches, "numbers": numbers}
