"""Push notifications to phones through Home Assistant notify services (e.g. mobile_app)."""

from __future__ import annotations

import logging
import time
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
    The title starts with the notification prefix (or the battery bank name) when one is set.
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
    if opts.notification_prefix:
        title = f"{opts.notification_prefix} · {title}"
    errors = []
    for service in opts.notify_services:
        try:
            name = await resolve_service(ha, service)
            try:
                await ha.call_service("notify", name, {"title": title, "message": message, "data": extra})
            except HAError as err:
                if err.kind != "http" or "400" not in str(err):
                    raise
                # Some notify integrations reject the phone options (tag, push, actions, url):
                # send the plain notification instead of none.
                await ha.call_service("notify", name, {"title": title, "message": message})
                _LOGGER.warning("notify.%s rejected the phone options (%s); sent without them", name, err)
        except HAError as err:
            _LOGGER.warning("Notification via notify.%s failed: %s", service, err)
            errors.append(f"notify.{service}: {err}")
    return errors


_services_cache: tuple[float, set[str]] = (0.0, set())


async def notify_services(ha: HomeAssistant) -> set[str]:
    """Names of Home Assistant's notify services (cached for 10 minutes)."""
    global _services_cache
    if time.time() - _services_cache[0] < 600 and _services_cache[1]:
        return _services_cache[1]
    domains = await ha.request("/services")
    names = set(next((d.get("services") or {} for d in domains if d.get("domain") == "notify"), {}))
    _services_cache = (time.time(), names)
    return names


async def resolve_service(ha: HomeAssistant, service: str) -> str:
    """The notify service to call: as configured, or mobile_app_<name> when only that exists
    (phones are notify.mobile_app_<device>). Raises HAError naming the available ones."""
    try:
        names = await notify_services(ha)
    except HAError:
        return service  # cannot check; just try it
    if service in names:
        return service
    if f"mobile_app_{service}" in names:
        _LOGGER.info("notify.%s does not exist; using notify.mobile_app_%s", service, service)
        return f"mobile_app_{service}"
    phones = sorted(n for n in names if n.startswith("mobile_app_"))
    available = ", ".join(f"notify.{n}" for n in (phones or sorted(names))[:8]) or "none"
    raise HAError("not_found", f"notify.{service} does not exist in Home Assistant (available: {available})")


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
        f"{str(result.get('plan_day') or 'tomorrow').capitalize()}: use {_num(result.get('predicted_consumption_tomorrow_kwh'))} kWh, "
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
