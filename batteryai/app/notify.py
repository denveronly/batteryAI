"""Push notifications to phones through Home Assistant notify services (e.g. mobile_app)."""

from __future__ import annotations

import logging
from typing import Any

from collector import program_ranges
from config import Options
from ha import HAError, HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def send(
    ha: HomeAssistant, opts: Options, title: str, message: str, *, kind: str = "info", url: str | None = None
) -> list[str]:
    """Sends a normal-priority (not critical) notification to every configured service.

    kind: notifications of the same kind replace each other on the phone (a new plan replaces
    the previous plan), different kinds stay side by side, grouped under BatteryAI.
    url: the BatteryAI panel; tapping the notification or its "Open BatteryAI" button opens it.
    Returns error messages; failures are logged but never stop the caller.
    """
    extra: dict[str, Any] = {
        "tag": f"batteryai-{kind}",
        "group": "batteryai",
        "push": {"interruption-level": "active"},
    }
    if url:
        extra["url"] = url  # iOS: tap opens this page
        extra["clickAction"] = url  # Android: tap opens this page
        extra["actions"] = [{"action": "URI", "title": "Open BatteryAI", "uri": url}]
    errors = []
    for service in opts.notify_services:
        try:
            await ha.call_service("notify", service, {"title": title, "message": message, "data": extra})
        except HAError as err:
            _LOGGER.warning("Notification via notify.%s failed: %s", service, err)
            errors.append(f"notify.{service}: {err}")
    return errors


def _num(value: Any, digits: int = 1) -> str:
    if value is None:
        return "?"
    text = f"{float(value):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def _program_lines(
    result: dict[str, Any], opts: Options, actions: list[dict[str, Any]], times: dict[Any, Any]
) -> list[str]:
    """One line per program in time order, with what changed on the inverter:
    "P1 00:00–03:00  54% · grid charge (was 100%, grid charge off)"."""
    was: dict[Any, list[str]] = {}
    for action in actions:
        if action.get("status") != "set" or not action.get("slot"):
            continue
        if action.get("kind") == "grid_charge":
            was.setdefault(action["slot"], []).append(f"grid charge {action.get('from') or '?'}")
        else:
            was.setdefault(action["slot"], []).insert(0, f"{_num(action.get('from'), 0)}%")
    programs = [
        {**p, "time": times.get(p["slot"]) or p.get("time")}  # the inverter's own times win
        for p in result.get("deye_programs") or [] if isinstance(p, dict) and p.get("slot") is not None
    ]
    ranges = program_ranges(programs, opts.program_time_marks == "end")
    lines = []
    for program in sorted(programs, key=lambda p: (ranges.get(p["slot"], (9999, 0))[0], p["slot"])):
        span = ranges.get(program["slot"])
        if span and span[0] == span[1]:
            continue  # unused (same time as another program)
        when = f"{_hhmm(span[0])}–{_hhmm(span[1])}" if span else str(program.get("time") or "")[:5]
        grid = " · grid charge" if program.get("grid_charge") else ""
        before = f" (was {', '.join(was[program['slot']])})" if program["slot"] in was else ""
        lines.append(f"P{program['slot']} {when}  {_num(program.get('soc_percent'), 0)}%{grid}{before}")
    return lines


def _change_lines(actions: list[dict[str, Any]]) -> list[str]:
    """Only real changes and failures."""
    lines = []
    for action in actions:
        prefix = f"P{action['slot']} " if action.get("slot") else ""
        if action.get("status") == "set":
            if action.get("kind") == "grid_charge":
                lines.append(f"{prefix}grid charge {action.get('from') or '?'} → {action.get('to')}")
            else:
                lines.append(f"{prefix}SOC {_num(action.get('from'), 0)}% → {_num(action.get('to'), 0)}%")
        elif action.get("status") == "error":
            lines.append(f"{prefix}failed: {action.get('error')}")
    return lines


def plan_notification(
    result: dict[str, Any], opts: Options, *, trigger: str, actions: list[dict[str, Any]] | None,
    current_programs: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """(title, message) for a new prediction. The first lines are a brief that fits the
    collapsed notification; pull it down (or long-press) for the programs and the changes.
    actions: what was written to the inverter (None when auto-control is off)."""
    risk = str(result.get("outage_risk") or "unknown")
    if trigger == "outage":
        title = "⚠️ BatteryAI: outage plan"
    elif risk in ("medium", "high"):
        title = f"⚠️ BatteryAI: new plan · outage risk {risk}"
    else:
        title = "🔋 BatteryAI: new plan"
    changes = _change_lines(actions or [])
    failures = [line for line in changes if "failed" in line]
    if actions is None:
        status = "Advice only – nothing was changed on the inverter."
    elif failures:
        status = f"⚠️ {len(failures)} change(s) could not be written to the inverter."
    elif changes:
        status = f"Written to the inverter ({len(changes)} change{'s' if len(changes) != 1 else ''})."
    else:
        status = "Inverter already matches the plan."

    brief = (
        f"Tomorrow: use {_num(result.get('predicted_consumption_tomorrow_kwh'))} kWh, "
        f"solar {_num(result.get('predicted_pv_tomorrow_kwh'))} kWh, "
        f"lowest battery {_num(result.get('predicted_min_soc_percent'), 0)}%."
    )
    summary = " ".join(str(result.get("summary") or "").split())
    if len(summary) > 400:
        summary = summary[:397] + "…"
    lines = [brief, status]
    if summary:
        lines += ["", summary]
    times = {p.get("slot"): p.get("time") for p in current_programs or [] if p.get("time")}
    programs = _program_lines(result, opts, actions or [], times)
    if programs:
        lines += ["", "Programs:", *programs]
    if failures:
        lines += ["", "Not written:", *failures]
    if risk not in ("medium", "high"):
        lines += ["", f"Outage risk: {risk}"]
    return title, "\n".join(lines)


def actions_message(actions: list[dict[str, Any]], intro: str | None = None) -> str | None:
    """intro plus the real changes and failures; None when there is nothing to tell."""
    lines = _change_lines(actions)
    if intro:
        lines = [intro, *([""] if lines else []), *lines]
    return "\n".join(lines) or None
