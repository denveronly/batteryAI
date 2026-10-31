"""Deye HV battery packs (BOS-G, BOS-G Pro …) read straight from the inverter's registers.

The Solarman integration's per-pack sensors are wrong for some HV batteries (ha-solarman
issue #1205). BatteryAI reads the same registers itself through the integration's
solarman.read_holding_registers action (it returns the raw values), decodes them with its
own checks and publishes the result as Home Assistant sensors with the prefix
deye_hv<N> (sensor.deye_hv1_battery_voltage, …), which the Battery health tab shows like
any other BMS.

Registers (holding, Deye three-phase HV hybrids SG01HP3 / SG04HP3):
  0x2712            number of battery packs reported by the BMS
  0x2730 + 38·(N−1) block of 38 registers for pack N (offsets in FIELDS)

The layout is the one ha-solarman uses. Where a value is implausible with the expected
scale, other scales are tried (SOH 0.1 % or 1 %, …); a value that stays implausible is
not published, and the raw registers are kept so the right layout can be worked out on
the Battery health tab (Deye HV registers).
"""

from __future__ import annotations

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
PACK_STRIDE = 38
MAX_PACKS = 16
MAX_READ = 125  # registers per Modbus request
PREFIX = "deye_hv"


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
    Field("battery_voltage", "Voltage", 8, (0.1, 0.01), 10, 1000, "V", "voltage", digits=2),
    Field("battery_current", "Current", 9, (0.1, 0.01), -600, 600, "A", "current", signed=True),
    Field("temperature", "Temperature", 10, (0.1,), -40, 90, "°C", "temperature", shift=1000),
    Field("max_cell_temperature", "Max cell temperature", 11, (0.1,), -40, 90, "°C", "temperature", shift=1000),
    Field("min_cell_temperature", "Min cell temperature", 12, (0.1,), -40, 90, "°C", "temperature", shift=1000),
    Field("mos_temperature", "MOS temperature", 13, (0.1,), -40, 120, "°C", "temperature", shift=1000),
    Field("battery_capacity_state_of_charge", "SOC", 15, (0.1, 1), 0, 100, "%", "battery", digits=0),
    Field("state_of_health", "SOH", 16, (0.1, 1, 0.01), 30, 100, "%", None, digits=0),
    Field("capacity", "Capacity", 18, (0.1, 1, 0.01), 20, 2000, "Ah", None),
    Field("max_cell_voltage", "Max cell voltage", 22, (0.001,), 2.0, 3.9, "V", "voltage", digits=3),
    Field("min_cell_voltage", "Min cell voltage", 23, (0.001,), 2.0, 3.9, "V", "voltage", digits=3),
    Field("charging_cycles", "Cycles", 24, (1,), 0, 20000, "", None, digits=0),
)
FIELD_AT = {f.offset: f for f in FIELDS}
EXTRA_NAMES = {
    **{i: "Serial number" for i in range(8)},
    25: "MOS state", 26: "Alarm (high)", 27: "Alarm (low)", 28: "Fault (high)", 29: "Fault (low)",
    30: "Software version", 31: "Hardware version",
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
    """One pack from its 38 registers."""
    values: dict[str, Any] = {}
    for f in FIELDS:
        values[f.key] = {**decode_field(f, regs[f.offset] if f.offset < len(regs) else None),
                         "name": f.name, "unit": f.unit, "register": PACK_BASE + PACK_STRIDE * (n - 1) + f.offset}
    v, i = values["battery_voltage"], values["battery_current"]
    power_ok = v["ok"] and i["ok"]
    values["battery_power"] = {"value": round(v["value"] * i["value"], 1) if power_ok else None, "ok": power_ok,
                               "name": "Power", "unit": "W", "scale": None, "register": None}
    hi, lo = values["max_cell_voltage"], values["min_cell_voltage"]
    delta_ok = hi["ok"] and lo["ok"] and hi["value"] >= lo["value"]
    values["cell_delta_voltage"] = {"value": round(hi["value"] - lo["value"], 3) if delta_ok else None, "ok": delta_ok,
                                    "name": "Cell delta", "unit": "V", "scale": None, "register": None}
    alarm = [regs[o] for o in (26, 27) if o < len(regs) and regs[o] is not None]
    fault = [regs[o] for o in (28, 29) if o < len(regs) and regs[o] is not None]
    raw_rows = []
    for offset, raw in enumerate(regs):
        f = FIELD_AT.get(offset)
        raw_rows.append({
            "register": PACK_BASE + PACK_STRIDE * (n - 1) + offset, "offset": offset, "raw": raw,
            "signed": None if raw is None else _signed(raw),
            "field": f.name if f else EXTRA_NAMES.get(offset),
            "value": values[f.key]["value"] if f else None, "ok": values[f.key]["ok"] if f else None,
        })
    return {
        "n": n, "prefix": f"{prefix}{n}", "serial": _serial(regs[0:8]), "values": values,
        "alarm": None if not alarm else any(alarm), "alarm_raw": alarm,
        "fault": None if not fault else any(fault), "fault_raw": fault,
        "registers": raw_rows,
        "empty": all(not r for r in regs),
    }


class DeyeHvReader:
    def __init__(self, ha: HomeAssistant, prefix: str = PREFIX, name: str = "HV battery") -> None:
        self.ha = ha
        self.prefix = prefix
        self.name = name
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

    async def _read(self, device: str, address: int, count: int) -> list[int | None]:
        response = await self.ha.service_response(
            "solarman", "read_holding_registers", {"device": device, "address": address, "count": count})
        data = response if isinstance(response, dict) else {}
        out: list[int | None] = []
        for reg in range(address, address + count):
            raw = data.get(str(reg), data.get(reg))
            out.append(int(raw) if isinstance(raw, (int, float)) else None)
        return out

    async def poll(self, entity_id: str, packs_setting: int) -> dict[str, Any]:
        """Reads all packs, publishes their sensors and returns what was read."""
        device = await self._device(entity_id)
        header = await self._read(device, HEADER, HEADER_COUNT)
        reported = header[PACK_COUNT_REG - HEADER]
        count = packs_setting or (reported if reported and 0 < reported <= MAX_PACKS else 0)
        if not count:
            raise HAError("http", f"the inverter reports {reported} battery packs (register 0x2712); "
                                  "set the number of packs in Settings → Batteries")
        regs: list[int | None] = []
        per_read = MAX_READ // PACK_STRIDE
        for first in range(0, count, per_read):
            n = min(per_read, count - first)
            regs += await self._read(device, PACK_BASE + PACK_STRIDE * first, PACK_STRIDE * n)
        packs = [decode_pack(i + 1, regs[i * PACK_STRIDE:(i + 1) * PACK_STRIDE], self.prefix) for i in range(count)]
        for p in packs:
            await self._publish(p)
        self.last = {
            "enabled": True, "time": time.time(), "entity": entity_id, "prefix": self.prefix, "name": self.name, "device_id": device,
            "reported_packs": reported, "packs": packs,
            "header": [{"register": HEADER + i, "raw": raw} for i, raw in enumerate(header)], "error": None,
        }
        return self.last

    async def _publish(self, p: dict[str, Any]) -> None:
        label = f"{self.name} pack {p['n']}"
        for key, v in p["values"].items():
            f = next((x for x in FIELDS if x.key == key), None)
            attrs: dict[str, Any] = {"friendly_name": f"{label} {v['name']}", "state_class": "measurement",
                                     "source": "BatteryAI (Deye HV registers)"}
            if v["unit"]:
                attrs["unit_of_measurement"] = v["unit"]
            device_class = f.device_class if f else {"battery_power": "power", "cell_delta_voltage": "voltage"}.get(key)
            if device_class:
                attrs["device_class"] = device_class
            if v.get("register") is not None:
                attrs["register"] = f"0x{v['register']:04X}"
            if key == "battery_voltage" and p["serial"]:
                attrs["serial_number"] = p["serial"]
            state = "unknown" if not v["ok"] or v["value"] is None else (
                f"{v['value']:.{f.digits}f}" if f else f"{v['value']:g}")
            await self.ha.set_state(f"sensor.{p['prefix']}_{key}", state, attrs)
        for key, on, raw in (("bms", p["alarm"], p["alarm_raw"]), ("fault", p["fault"], p["fault_raw"])):
            await self.ha.set_state(
                f"binary_sensor.{p['prefix']}_alarm_{key}",
                "unknown" if on is None else ("on" if on else "off"),
                {"friendly_name": f"{label} {'BMS alarm' if key == 'bms' else 'BMS fault'}", "device_class": "problem",
                 "raw": " ".join(f"0x{r:04X}" for r in raw)})
