"""Push notifications to phones through Home Assistant notify services (e.g. mobile_app)."""

from __future__ import annotations

import logging
from typing import Any

from config import Options
from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def send(ha: HomeAssistant, opts: Options, title: str, message: str) -> list[str]:
    """Sends a normal-priority (not critical) notification to every configured service.

    Returns error messages; failures are logged but never stop the caller.
    """
    errors = []
    for service in opts.notify_services:
        data: dict[str, Any] = {
            "title": title,
            "message": message,
            # Same tag/group: a newer BatteryAI message replaces or groups with the previous one.
            "data": {"tag": "batteryai", "group": "batteryai", "push": {"interruption-level": "active"}},
        }
        try:
            await ha.call_service("notify", service, data)
        except HAError as err:
            _LOGGER.warning("Notification via notify.%s failed: %s", service, err)
            errors.append(f"notify.{service}: {err}")
    return errors


def _num(value: Any, digits: int = 1) -> str:
    if value is None:
        return "?"
    text = f"{float(value):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def prediction_message(result: dict[str, Any]) -> str:
    parts = [
        f"Tomorrow {_num(result.get('predicted_consumption_tomorrow_kwh'))} kWh",
        f"PV {_num(result.get('predicted_pv_tomorrow_kwh'))} kWh",
        f"min SOC {_num(result.get('predicted_min_soc_percent'), 0)}%",
        f"outage risk {result.get('outage_risk', '?')}",
    ]
    programs = " ".join(
        f"P{p.get('slot')} {p.get('time')} {_num(p.get('soc_percent'), 0)}%{' ⚡' if p.get('grid_charge') else ''}"
        for p in result.get("deye_programs") or []
    )
    summary = (result.get("summary") or "").strip()
    if len(summary) > 220:
        summary = summary[:217] + "…"
    return " · ".join(parts) + (f"\n{programs}" if programs else "") + (f"\n{summary}" if summary else "")


def actions_message(actions: list[dict[str, Any]]) -> str | None:
    """Only real changes and failures; returns None when nothing changed."""
    lines = []
    for action in actions:
        prefix = f"P{action['slot']} " if action.get("slot") else ""
        if action.get("status") == "set":
            if action.get("kind") == "grid_charge":
                lines.append(f"{prefix}force charge {action.get('from') or '?'} → {action.get('to')}")
            else:
                lines.append(f"{prefix}SOC {_num(action.get('from'), 0)}% → {_num(action.get('to'), 0)}%")
        elif action.get("status") == "error":
            lines.append(f"{prefix}failed: {action.get('error')}")
    return "\n".join(lines) or None
