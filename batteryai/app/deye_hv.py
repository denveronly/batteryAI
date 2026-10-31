"""Deye HV batteries (BOS-G, BOS-G Pro …) read straight from the inverter's registers.

The Solarman integration's per-pack sensors are wrong for BOS-G (ha-solarman issue #1205):
it cuts the battery data into 38-register "packs", but a BOS-G stack (PDU + modules) is one
record of about 80 registers. BatteryAI reads the registers itself through the integration's
solarman.read_holding_registers action, decodes the stack record and publishes Home
Assistant sensors with the battery's prefix and the stack number
(sensor.deye_hv1_battery_voltage, …), shown on the Battery health tab like any other BMS.

Registers (holding, Deye three-phase HV hybrids; decimal 10000 = 0x2710), a stack record from
10032 (0x2730), the second battery bank 5000 registers further (15032):
  10032–10039 serial (ASCII)         10046 cycles            10047 voltage 0.1 V
  10048 current 0.1 A, offset 30000 (negative = charging)  10049 BMS temp, offset 40   10050 SOC %   10051 SOH %
  10052 highest cell mV (10053 module, 10054 cell)   10055 lowest cell mV (10056 module, 10057 cell)
  10058 / 10059 / 10062 average / highest / lowest cell temperature, offset 40
  10065 / 10066 charge / discharge current limit 0.1 A      10070 number of modules
  10077+10078 / 10079+10080 total charge / discharge energy (kWh, high word first)
  0x2712 = number of banks. Inverter registers 0x00D2… (bank 2: +31) carry what the BMS sends to
  the inverter, among them the battery alarm 0x00DC and fault 0x00DD.

A value implausible with the expected scale is not published; every raw register is kept for
the Battery health tab so the layout can be checked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)

HEADER = 0x2710
HEADER_COUNT = 32
PACK_COUNT_REG = 0x2712
PACK_BASE = 0x2730
PACK_STRIDE = 5000  # the next battery bank
RECORD = 82        # registers of one stack record (10032–10113)
MAX_PACKS = 2  # Deye HV inverters have at most two battery / BMS ports
MAX_READ = 42  # registers per request: Home Assistant waits only ~10 s for an action's answer
READ_TRIES = 3
PREFIX = "deye_hv"
# What the BMS reports to the inverter (CAN), inverter registers 0x00D2–0x00DF: limits and the
# battery alarm / fault flags (any bit set = a problem, as the Solarman integration reads them).
INV_BMS = 0x00D2
INV_BMS_COUNT = 14
INV_BMS_STRIDE = 31  # bank 2 at 0x00F1…


@dataclass(frozen=True)
class Field:
    key: str                 # entity suffix (sensor.deye_hvN_<key>)
    name: str
    offset: int              # register within the pack block
    scales: tuple[float, ...]  # the first that gives a plausible value wins
    low: float
    high: float
    unit: str = ""
    device_class: str | None = None
    signed: bool = False
    shift: int = 0           # subtracted from the raw value before scaling (temperatures: 1000)
    digits: int = 1


FIELDS: tuple[Field, ...] = (
    Field("charging_cycles", "Cycles", 14, (1,), 0, 20000, "", None, digits=0),
    Field("battery_voltage", "Voltage", 15, (0.1,), 10, 1000, "V", "voltage", digits=1),
    # The BMS counts charging negative; published positive = charging (as the DIY batteries).
    Field("battery_current", "Current", 16, (-0.1,), -1000, 1000, "A", "current", shift=30000),
    Field("bms_temperature", "BMS temperature", 17, (1,), -40, 100, "°C", "temperature", shift=40, digits=0),
    Field("battery_capacity_state_of_charge", "SOC", 18, (1,), 0, 100, "%", "battery", digits=0),
    Field("state_of_health", "SOH", 19, (1,), 30, 100, "%", None, digits=0),
    Field("max_cell_voltage", "Highest cell", 20, (0.001,), 2.0, 3.9, "V", "voltage", digits=3),
    Field("min_cell_voltage", "Lowest cell", 23, (0.001,), 2.0, 3.9, "V", "voltage", digits=3),
    Field("temperature", "Cell temperature", 26, (1,), -40, 90, "°C", "temperature", shift=40, digits=0),
    Field("max_cell_temperature", "Highest cell temperature", 27, (1,), -40, 90, "°C", "temperature", shift=40, digits=0),
    Field("min_cell_temperature", "Lowest cell temperature", 30, (1,), -40, 90, "°C", "temperature", shift=40, digits=0),
    Field("charge_current_limit", "Charge current limit", 33, (0.1,), 0, 1000, "A", "current"),
    Field("discharge_current_limit", "Discharge current limit", 34, (0.1,), 0, 1000, "A", "current"),
    Field("module_count", "Modules", 38, (1,), 1, 32, "", None, digits=0),
)
INV_FIELDS: tuple[Field, ...] = (
    Field("bms_charge_voltage", "BMS charge voltage", 0, (0.1, 0.01), 40, 1000, "V", "voltage"),
    Field("bms_discharge_voltage", "BMS discharge voltage", 1, (0.1, 0.01), 40, 1000, "V", "voltage"),
)
INV_NAMES = {0: "BMS charge voltage", 1: "BMS discharge voltage", 2: "Charge current", 3: "Discharge current",
             4: "BMS SOC", 5: "BMS voltage", 6: "BMS current", 8: "Max charging current",
             9: "Max discharging current", 10: "Battery alarm", 11: "Battery fault", 12: "BMS other symbol"}
# Checked against Solar Assistant / a documented map; the others are marked "to be confirmed".
CONFIRMED = {f.key for f in FIELDS} - {"bms_temperature"}
FIELD_AT = {f.offset: f for f in FIELDS}
EXTRA_NAMES = {
    **{i: "Serial number" for i in range(8)},
    21: "Highest cell: module", 22: "Highest cell: cell", 24: "Lowest cell: module", 25: "Lowest cell: cell",
    28: "Highest temp: module", 29: "Highest temp: sensor", 31: "Lowest temp: module", 32: "Lowest temp: sensor",
    45: "Charge energy (high word)", 46: "Charge energy (low word)",
    47: "Discharge energy (high word)", 48: "Discharge energy (low word)",
    49: "a voltage, 0.1 V", 50: "a voltage, 0.1 V", 51: "a voltage, 0.1 V",
    78: "Alarm word 1", 79: "Alarm word 2", 80: "Fault word 1", 81: "Fault word 2",
}


def _signed(raw: int) -> int:
    return raw - 0x10000 if raw >= 0x8000 else raw


def decode_field(f: Field, raw: int | None) -> dict[str, Any]:
    """{"value", "scale", "ok"}: the first scale that gives a plausible value."""
    if raw is None:
        return {"value": None, "scale": None, "ok": False}
    base = (_signed(raw) if f.signed else raw) - f.shift
    for scale in f.scales:
        value = round(base * scale, 4)
        if f.low <= value <= f.high:
            return {"value": value, "scale": scale, "ok": True}
    return {"value": round(base * f.scales[0], 4), "scale": f.scales[0], "ok": False}


def _serial(regs: list[int | None]) -> str:
    chars = []
    for reg in regs:
        if reg is None:
            continue
        for byte in (reg >> 8, reg & 0xFF):
            if 32 <= byte < 127:
                chars.append(chr(byte))
    return "".join(chars).strip()


def decode_pack(n: int, regs: list[int | None], prefix: str = PREFIX) -> dict[str, Any]:
    """One stack from its record."""
    values: dict[str, Any] = {}
    for f in FIELDS:
        values[f.key] = {**decode_field(f, regs[f.offset] if f.offset < len(regs) else None),
                         "name": f.name, "unit": f.unit, "register": PACK_BASE + PACK_STRIDE * (n - 1) + f.offset,
                         "confirmed": f.key in CONFIRMED}
    pos = lambda o: regs[o] if o < len(regs) else None
    for key, hi_o, lo_o, name in (("total_charge_energy", 45, 46, "Total charge"), ("total_discharge_energy", 47, 48, "Total discharge")):
        hi_w, lo_w = pos(hi_o), pos(lo_o)
        ok = hi_w is not None and lo_w is not None
        values[key] = {"value": hi_w * 65536 + lo_w if ok else None, "ok": ok, "name": name, "unit": "kWh",
                       "scale": 1, "register": PACK_BASE + PACK_STRIDE * (n - 1) + hi_o, "confirmed": False,
                       "state_class": "total_increasing"}
    v, i = values["battery_voltage"], values["battery_current"]
    power_ok = v["ok"] and i["ok"]
    values["battery_power"] = {"value": round(v["value"] * i["value"], 1) if power_ok else None, "ok": power_ok,
                               "name": "Power", "unit": "W", "scale": None, "register": None}
    hi, lo = values["max_cell_voltage"], values["min_cell_voltage"]
    delta_ok = hi["ok"] and lo["ok"] and hi["value"] >= lo["value"]
    values["cell_delta_voltage"] = {"value": round(hi["value"] - lo["value"], 3) if delta_ok else None, "ok": delta_ok,
                                    "name": "Cell delta", "unit": "V", "scale": None, "register": None}
    values["max_cell_voltage"].update(module=pos(21), position=pos(22))
    values["min_cell_voltage"].update(module=pos(24), position=pos(25))
    values["max_cell_temperature"].update(module=pos(28), position=pos(29))
    values["min_cell_temperature"].update(module=pos(31), position=pos(32))
    # The alarm / fault words of the record (10110–10113) are shown raw; alarms come from what the
    # BMS sends to the inverter (0x00DC / 0x00DD), see decode_inverter_bms.
    alarm: list[int] = []
    fault: list[int] = []
    raw_rows = []
    for offset, raw in enumerate(regs):
        f = FIELD_AT.get(offset)
        raw_rows.append({
            "register": PACK_BASE + PACK_STRIDE * (n - 1) + offset, "offset": offset, "raw": raw,
            "signed": None if raw is None else _signed(raw),
            "field": (f.name + ("" if f.key in CONFIRMED else " (to be confirmed)")) if f else EXTRA_NAMES.get(offset),
            "value": values[f.key]["value"] if f else None, "ok": values[f.key]["ok"] if f else None,
        })
    return {
        "n": n, "prefix": f"{prefix}{n}", "serial": _serial(regs[0:8]), "values": values,
        "alarm": None if not alarm else any(alarm), "alarm_raw": alarm,
        "fault": None if not fault else any(fault), "fault_raw": fault,
        "registers": raw_rows,
        "empty": all(not r for r in regs),
    }


def decode_inverter_bms(regs: list[int | None], base: int = INV_BMS) -> dict[str, Any]:
    """Limits and alarm / fault flags the BMS sends to the inverter (0x00D2…)."""
    values: dict[str, Any] = {}
    for f in INV_FIELDS:
        values[f.key] = {**decode_field(f, regs[f.offset] if f.offset < len(regs) else None),
                         "name": f.name, "unit": f.unit, "register": base + f.offset, "confirmed": False}
    if len(regs) < 12 or regs[10] is None or regs[11] is None:
        alarm = fault = None
        alarm_raw: list[int] = []
        fault_raw: list[int] = []
    else:
        alarm_raw, fault_raw = [regs[10]], [regs[11]]
        alarm, fault = bool(regs[10]), bool(regs[11])
    rows = [{"register": base + i, "offset": i, "raw": raw, "signed": None if raw is None else _signed(raw),
             "field": INV_NAMES.get(i), "value": None, "ok": None} for i, raw in enumerate(regs)]
    for f in INV_FIELDS:
        if f.offset < len(rows):
            rows[f.offset].update(value=values[f.key]["value"], ok=values[f.key]["ok"])
    return {"values": values if regs else {}, "alarm": alarm, "alarm_raw": alarm_raw,
            "fault": fault, "fault_raw": fault_raw, "registers": rows}


class DeyeHvReader:
    def __init__(self, ha: HomeAssistant, prefix: str = PREFIX, name: str = "HV battery") -> None:
        self.ha = ha
        self.prefix = prefix
        self.name = name
        self._cleared: set[str] = set()
        self._failures = 0
        self.device_id: str | None = None
        self._device_for: str | None = None
        self.last: dict[str, Any] = {"enabled": False}

    async def _device(self, entity_id: str) -> str:
        if self.device_id and self._device_for == entity_id:
            return self.device_id
        device = (await self.ha.render_template("{{ device_id('%s') }}" % entity_id)).strip()
        if not device or device == "None":
            raise HAError("not_found", f"{entity_id} does not belong to a device in Home Assistant")
        self.device_id, self._device_for = device, entity_id
        return device

    async def _read(self, device: str, address: int, count: int, tries: int = READ_TRIES) -> list[int | None]:
        """Registers in requests of at most MAX_READ, each retried when Home Assistant gives up
        waiting for the Solarman logger (HTTP 400 "… a long duration of time …") or it times out."""
        out: list[int | None] = []
        for start in range(address, address + count, MAX_READ):
            n = min(MAX_READ, address + count - start)
            for attempt in range(1, tries + 1):
                try:
                    response = await self.ha.service_response(
                        "solarman", "read_holding_registers", {"device": device, "address": start, "count": n})
                    break
                except HAError as err:
                    if attempt == tries or err.kind == "auth":
                        raise
                    _LOGGER.debug("Registers 0x%04X×%d, try %d: %s", start, n, attempt, err)
                    await asyncio.sleep(3 * attempt)  # let the logger finish what it is doing
            data = response if isinstance(response, dict) else {}
            for reg in range(start, start + n):
                raw = data.get(str(reg), data.get(reg))
                out.append(int(raw) if isinstance(raw, (int, float)) else None)
        return out

    async def poll(self, entity_id: str, packs_setting: int) -> dict[str, Any]:
        """Reads all packs, publishes their sensors and returns what was read."""
        device = await self._device(entity_id)
        header = await self._read(device, PACK_COUNT_REG, 1)
        reported = header[0]
        count = min(MAX_PACKS, packs_setting or (reported if reported and reported > 0 else 1))
        packs = []
        for i in range(count):
            try:
                regs = await self._read(device, PACK_BASE + PACK_STRIDE * i, RECORD, tries=READ_TRIES if i == 0 else 1)
            except HAError:
                if i == 0:
                    raise
                _LOGGER.info("No second battery bank answers at %d – skipped", PACK_BASE + PACK_STRIDE * i)
                continue
            p = decode_pack(i + 1, regs, self.prefix)
            if p["empty"]:
                continue  # a bank that does not exist
            try:
                inv = await self._read(device, INV_BMS + INV_BMS_STRIDE * i, INV_BMS_COUNT)
            except HAError as err:
                _LOGGER.info("BMS registers 0x%04X not read: %s", INV_BMS + INV_BMS_STRIDE * i, err)
                inv = []
            block = decode_inverter_bms(inv, INV_BMS + INV_BMS_STRIDE * i)
            p["values"].update(block["values"])
            p.update(alarm=block["alarm"], alarm_raw=block["alarm_raw"], fault=block["fault"],
                     fault_raw=block["fault_raw"], inverter_bms=block["registers"])
            packs.append(p)
        self._failures = 0
        for p in packs:
            await self._publish(p)
            await self.ha.set_state(f"binary_sensor.{p['prefix']}_status_online", "on",
                                    {"friendly_name": f"{self.name} online", "device_class": "connectivity"})
            if p["prefix"] not in self._cleared:
                # Sensors of the 0.4.63–0.4.65 pack layout that no longer exist.
                for key in ("capacity", "mos_temperature"):
                    await self.ha.set_state(f"sensor.{p['prefix']}_{key}", "unavailable", {"friendly_name": "(no longer used)"})
                for key in ("bms", "fault"):  # the 0.4.63/0.4.64 alarm sensors (wrong registers)
                    await self.ha.set_state(f"binary_sensor.{p['prefix']}_alarm_{key}", "unavailable", {"friendly_name": "(no longer used)"})
                self._cleared.add(p["prefix"])
        self.last = {
            "enabled": True, "time": time.time(), "entity": entity_id, "prefix": self.prefix, "name": self.name, "device_id": device,
            "reported_packs": reported, "packs": packs,
            "header": [{"register": PACK_COUNT_REG, "raw": reported}], "error": None,
        }
        return self.last

    async def failed(self, after: int) -> None:
        """A read failed: after this many in a row the stacks are reported offline (an alarm)."""
        self._failures += 1
        if self._failures != after:
            return
        for p in self.last.get("packs") or []:
            try:
                await self.ha.set_state(f"binary_sensor.{p['prefix']}_status_online", "off",
                                        {"friendly_name": f"{self.name} online", "device_class": "connectivity"})
            except HAError:
                pass

    async def _publish(self, p: dict[str, Any]) -> None:
        label = self.name if p["n"] == 1 else f"{self.name} stack {p['n']}"
        for key, v in p["values"].items():
            f = next((x for x in FIELDS if x.key == key), None)
            attrs: dict[str, Any] = {"friendly_name": f"{label} {v['name']}", "state_class": v.get("state_class", "measurement"),
                                     "source": "BatteryAI (Deye HV registers)"}
            if v["unit"]:
                attrs["unit_of_measurement"] = v["unit"]
            device_class = f.device_class if f else {"battery_power": "power", "cell_delta_voltage": "voltage",
                                                     "total_charge_energy": "energy", "total_discharge_energy": "energy"}.get(key)
            if device_class:
                attrs["device_class"] = device_class
            if v.get("register") is not None:
                attrs["register"] = f"0x{v['register']:04X}"
            if key == "battery_voltage" and p["serial"]:
                attrs["serial_number"] = p["serial"]
            if v.get("position") is not None:
                attrs["position"] = v["position"]
            if v.get("module") is not None:
                attrs["module"] = v["module"]
            if v.get("confirmed") is False:
                attrs["note"] = "register not confirmed yet"
            state = "unknown" if not v["ok"] or v["value"] is None else (
                f"{v['value']:.{f.digits}f}" if f else f"{v['value']:g}")
            await self.ha.set_state(f"sensor.{p['prefix']}_{key}", state, attrs)
        # Battery alarm / fault as the BMS reports them to the inverter (0x00DC / 0x00DD).
        for key, on, raw in (("bms_alarm", p["alarm"], p["alarm_raw"]), ("bms_fault", p["fault"], p["fault_raw"])):
            if on is None:
                continue
            await self.ha.set_state(
                f"binary_sensor.{p['prefix']}_alarm_{key}", "on" if on else "off",
                {"friendly_name": f"{label} {'battery alarm' if key == 'bms_alarm' else 'battery fault'}",
                 "device_class": "problem", "raw": " ".join(f"0x{r:04X}" for r in raw)})
