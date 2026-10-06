"""BatteryAI add-on: records sensors, runs scheduled AI analyses, serves the ingress UI."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import shutil
from collections import deque
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import anthropic
from aiohttp import web

from analyzer import AnalysisError, analyze, build_input
import control
import local_fast
import local_llm
import notify
import outage_plan
import openai_engine
import backup
from bill import BillRecorder, bill_report, reprice_month
from collector import UNAVAILABLE, active_program, collect, has_data, parse_hhmm, to_float, weather_details
from config import DATA_DIR, Options, SettingsError, load_settings, parse_settings, save_settings
from db import READING_FIELDS, Database, accuracy_report, economy_report, monthly_summary
from ha import HAError, HomeAssistant, container_env
from history import import_history

PORT = int(os.environ.get("BATTERYAI_PORT", "8099"))
STATIC_DIR = Path(__file__).parent / "static"
MAX_CHART_POINTS = 2500
OUTAGE_RERUN_SECONDS = 30 * 60  # at most one extra prediction per 30 min when outages change
REQUIRED_SENSORS = ("today_forecast", "tomorrow_forecast", "battery_soc", "load_power", "today_consumption")

_LOGGER = logging.getLogger("batteryai")


class BatteryAI:
    def __init__(self, opts: Options, db: Database, ha: HomeAssistant, tz: tzinfo) -> None:
        self.opts = opts
        self.db = db
        self.ha = ha
        self.tz = tz
        self.next_analysis: datetime | None = None
        self.tz_source = "home_assistant"
        self.last_snapshot: dict[str, Any] | None = None
        self._analysis_lock = asyncio.Lock()
        self._loops: list[asyncio.Task] = []
        self._client = self._make_client(opts.claude_api_key)
        self.control = control.load_state()
        self.history_import: dict[str, Any] = {"running": False, "result": None, "error": None}
        self._background: set[asyncio.Task] = set()
        self._last_outage_key: str | None = None
        self._last_compress = 0.0
        self.model_download = local_llm.ModelDownloader()
        self.panel_url: str | None = None  # opened by tapping a notification
        self._last_outage_run = 0.0
        self.bill = BillRecorder(db)
        self.outage_minutes: float | None = None  # from the outage minutes sensor
        self.outage_minutes_ts: float | None = None
        self._outage_lock = asyncio.Lock()
        self.outage_plan: dict[str, Any] | None = None  # the latest tariff-aware decision
        self.outage_duration: float | None = None
        self.emergency = False  # emergency outages in effect (emergency outage sensor)
        self._profile: tuple[float, dict[tuple[int, int], float]] = (0.0, {})

    def spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # History import -------------------------------------------------------

    def start_history_import(self, days: int) -> bool:
        if self.history_import["running"]:
            return False
        self.history_import = {"running": True, "days": days, "result": None, "error": None}
        self.spawn(self._run_history_import(days))
        return True

    async def _run_history_import(self, days: int) -> None:
        try:
            result = await import_history(self.ha, self.opts, self.db, self.tz, days, self.history_import)
            self.history_import["result"] = result
        except Exception as err:
            _LOGGER.exception("History import failed")
            self.history_import["error"] = str(err)
        finally:
            self.history_import["running"] = False
            self.history_import["finished"] = time.time()

    def import_history_if_empty(self) -> None:
        """On a fresh install there is no history yet: take it from the Home Assistant recorder."""
        earliest = self.db.earliest_ts()
        if earliest is None or earliest > time.time() - 86400:
            _LOGGER.info("Less than a day of readings; importing history from Home Assistant")
            self.start_history_import(self.opts.history_days)

    # Inverter control -------------------------------------------------------

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        previous = self.control["mode"]
        if previous == "charge_all" and mode != "charge_all":
            actions += await control.restore_switches(self.ha, self.control)
        self.control.update(mode=mode, since=time.time())
        control.save_state(self.control)
        _LOGGER.info("Control mode %s -> %s", previous, mode)
        if mode == "auto":
            latest = next((a for a in self.db.analyses(20) if a["status"] == "ok" and a["result"]), None)
            if latest:
                applied = await control.apply_prediction(self.ha, self.opts, latest["result"])
                self.db.add_actions(latest["id"], applied)
                actions += applied
        await self.notify_actions(
            actions,
            {"auto": "AI auto-control on", "off": "AI auto-control off"}.get(mode, "SOC updated"),
            {
                "auto": "The latest plan is written to the inverter after every prediction.",
                "off": "Predictions only advise; nothing is written to the inverter.",
            }.get(mode),
        )
        return actions

    async def charge_all(self) -> list[dict[str, Any]]:
        actions = await control.charge_all(self.ha, self.opts, self.control)
        self.control.update(mode="charge_all", since=time.time(), last_actions=actions, precharge=None)
        control.save_state(self.control)
        _LOGGER.info("Charge all to %d%%: %s", self.opts.charge_all_soc_percent, actions)
        await self.notify_actions(actions, f"charging all to {self.opts.charge_all_soc_percent}%")
        return actions

    # Backup and restore -------------------------------------------------------

    async def restore(self) -> None:
        """Swaps in the unpacked backup (backup.check_backup ran before): database,
        settings and control state, then restarts recording and the schedule."""
        self.stop_loops()
        db_path = os.path.join(DATA_DIR, backup.DB_NAME)
        self.db.close()
        try:
            await asyncio.to_thread(backup.install_restore)
        finally:
            self.db = Database(db_path)
            self.bill = BillRecorder(self.db)
        self.control = control.load_state()
        self.opts = load_settings()
        self._client = self._make_client(self.opts.claude_api_key)
        await asyncio.to_thread(self.relocalize)
        self.start_loops()
        _LOGGER.info("Backup restored: %d readings", self.db.reading_count())

    # Charge before an outage --------------------------------------------------

    async def read_outage_minutes(self) -> float | None:
        """Minutes until the next outage from the outage minutes sensor (h and s are converted)."""
        if not self.opts.outage_minutes_sensor:
            self.outage_minutes = None
            return None
        state = await self.ha.state(self.opts.outage_minutes_sensor)
        value = to_float(state.get("state")) if state else None
        unit = str(((state or {}).get("attributes") or {}).get("unit_of_measurement") or "").strip().lower()
        if value is not None:
            value = value * 60 if unit in ("h", "hours") else value / 60 if unit in ("s", "sec", "seconds") else value
        self.outage_minutes, self.outage_minutes_ts = value, time.time()
        return value

    async def check_outage(self) -> None:
        async with self._outage_lock:
            await self._check_outage()

    async def _check_outage(self) -> None:
        """Outage within outage_precharge_minutes: every program goes to the pre-outage SOC
        with grid charge on. When the sensor points to a later outage again,
        the previous mode (and in advice-only mode the previous SOC values) comes back."""
        minutes = await self.read_outage_minutes()
        now = time.time()
        threshold = self.opts.outage_precharge_minutes
        running = self.control.get("precharge")
        if await self._check_emergency(now, running):
            return
        if running:
            if self.control["mode"] != "charge_all":
                # Changed by hand (auto-control or Charge all): the pre-outage charge is over.
                self.control["precharge"] = None
                control.save_state(self.control)
                return
            outage_at = now + minutes * 60 if minutes is not None else None
            moved_on = minutes is not None and minutes > threshold and outage_at > running["outage_at"] + 1800
            if moved_on or now > running["outage_at"] + 12 * 3600:
                await self.end_precharge(running)
            return
        if minutes is not None and minutes > 0:
            await self.tariff_aware_plan(now, now + minutes * 60)  # shown on the dashboard
        else:
            self.outage_plan = None
        if minutes is None or not self.control.get("precharge_enabled", True) or not 0 < minutes <= threshold:
            return
        if self.control["mode"] == "charge_all" or not any(p.soc_entity for p in self.opts.deye_programs):
            return
        outage_at = now + minutes * 60
        done = self.control.get("precharge_done_for")
        if done and abs(outage_at - done) < 1800:
            return  # already charged for this outage (and stopped by hand)
        soc = self.opts.outage_precharge_soc_percent
        reason = f"An outage is expected in {minutes:.0f} minutes; all programs are set to {soc}% with grid charge on."
        decision = await self.tariff_aware_plan(now, outage_at)
        if self.emergency:
            reason = f"Emergency outages are on: tariffs are ignored and all programs are set to {soc}% with grid charge on (outage in {minutes:.0f} minutes)."
        if decision is not None:
            if not decision["charge"]:
                return  # checked again every minute (the SOC may drop)
            soc = decision["target_soc"]
            reason = decision["reason"]
        previous_mode = self.control["mode"]
        socs = await control.read_socs(self.ha, self.opts) if previous_mode == "off" else {}
        actions = await control.charge_all(self.ha, self.opts, self.control, soc)
        self.control.update(
            mode="charge_all", since=now, last_actions=actions, precharge_done_for=outage_at,
            precharge={"outage_at": outage_at, "previous_mode": previous_mode, "socs": socs, "soc": soc, "started": now},
        )
        control.save_state(self.control)
        at = datetime.fromtimestamp(outage_at, self.tz).strftime("%H:%M")
        _LOGGER.info("Outage in %.0f minutes (%s): charging every program to %d%%: %s", minutes, at, soc, actions)
        await self.notify_actions(actions, f"outage at {at}, charging to {soc}%", reason)

    async def _check_emergency(self, now: float, running: dict[str, Any] | None) -> bool:
        """Emergency outages (emergency outage sensor on): with "Charge on emergency outages"
        every program goes to the pre-outage SOC with grid charge on right away, and the
        outage schedule and AI predictions are ignored until the sensor turns off; then the
        previous mode (and SOC values) come back. Returns True when it handled this check."""
        sensor = self.opts.emergency_outage_sensor
        self.emergency = bool(sensor) and outage_plan.is_on(await self.ha.state(sensor))
        if running and running.get("emergency"):
            if self.control["mode"] != "charge_all":
                self.control["precharge"] = None  # stopped by hand; not again until it turns off
                control.save_state(self.control)
            elif not self.emergency:
                _LOGGER.info("Emergency outages are over")
                self.control.pop("emergency_done", None)
                await self.end_precharge(running)
            return True
        if not self.emergency:
            if self.control.pop("emergency_done", None):
                control.save_state(self.control)
            return False
        if (
            not self.control.get("emergency_enabled", True)
            or self.control.get("emergency_done")
            or not any(p.soc_entity for p in self.opts.deye_programs)
            or (self.control["mode"] == "charge_all" and not running)  # Charge all by hand
        ):
            return False
        soc = self.opts.outage_precharge_soc_percent
        if running:  # a scheduled pre-outage charge becomes the emergency charge
            previous_mode, socs = running.get("previous_mode") or "off", running.get("socs") or {}
        else:
            previous_mode = self.control["mode"]
            socs = await control.read_socs(self.ha, self.opts) if previous_mode == "off" else {}
        actions = await control.charge_all(self.ha, self.opts, self.control, soc)
        self.control.update(
            mode="charge_all", since=now, last_actions=actions, emergency_done=True,
            precharge={"emergency": True, "outage_at": now, "previous_mode": previous_mode, "socs": socs, "soc": soc, "started": now},
        )
        control.save_state(self.control)
        _LOGGER.info("Emergency outages: charging every program to %d%%: %s", soc, actions)
        await self.notify_actions(
            actions, f"emergency outages, charging to {soc}%",
            f"Emergency outages are on: all programs are set to {soc}% with grid charge on. The outage schedule "
            "and AI predictions are ignored until emergency outages end.",
        )
        return True

    async def read_outage_duration(self) -> float | None:
        state = await self.ha.state(self.opts.outage_duration_sensor) if self.opts.outage_duration_sensor else None
        self.outage_duration = outage_plan.duration_minutes(state)
        return self.outage_duration

    async def tariff_aware_plan(self, now: float, outage_at: float) -> dict[str, Any] | None:
        """The tariff-aware decision, or None when it does not apply (switched off, a single
        price, or no outage duration): then the battery is charged to the pre-outage SOC."""
        self.outage_plan = None
        if not self.control.get("precharge_smart", True) or self.opts.single_price:
            return None
        if self.emergency:
            return None  # emergency outages: ignore tariffs, charge to the pre-outage SOC
        duration = await self.read_outage_duration()
        if not duration:
            return None
        if time.time() - self._profile[0] > 3600:
            profile = await asyncio.to_thread(self.db.hourly_profile, int(now) - 14 * 86400)
            self._profile = (time.time(), outage_plan.load_profile(profile))
        soc = (self.last_snapshot or {}).get("battery_soc")
        self.outage_plan = outage_plan.plan(self.opts, self.tz, now, outage_at, duration, self._profile[1], soc)
        return self.outage_plan

    async def end_precharge(self, running: dict[str, Any]) -> None:
        previous = running.get("previous_mode") or "off"
        self.control["precharge"] = None
        actions = await self.set_mode(previous)
        if previous == "off" and running.get("socs"):
            restored = await control.restore_socs(self.ha, self.opts, running["socs"])
            _LOGGER.info("Pre-outage charge over; SOC values restored: %s", restored)
            await self.notify_actions(restored, "pre-outage charge over", "The SOC values from before the outage are back.")
        _LOGGER.info("Pre-outage charge over; back to %s (%d changes)", previous, len(actions))

    async def outage_loop(self) -> None:
        while True:
            try:
                await self.check_outage()
            except Exception:
                _LOGGER.exception("Checking the outage minutes sensor failed")
            await asyncio.sleep(60)

    @staticmethod
    def _make_client(api_key: str) -> anthropic.AsyncAnthropic | None:
        return anthropic.AsyncAnthropic(api_key=api_key, max_retries=3, timeout=600.0) if api_key else None

    def start_loops(self) -> None:
        self._loops = [
            asyncio.create_task(self.recorder_loop()),
            asyncio.create_task(self.scheduler_loop()),
            asyncio.create_task(self.outage_loop()),
        ]

    def stop_loops(self) -> None:
        for task in self._loops:
            task.cancel()
        self._loops = []

    def relocalize(self) -> None:
        """Re-derives local date, time of day and weekday of stored readings for the current
        time zone and weekend days (fixes rows recorded while the time zone was wrong)."""
        changed = self.db.relocalize(self.tz, self.opts.weekend_days)
        if changed:
            _LOGGER.info("Corrected local date/time of %d readings for %s", changed, self.tz)
        self.recompute_targets()

    async def retry_time_zone(self) -> None:
        """Keeps asking Home Assistant for its time zone until it answers, then switches to it."""
        while True:
            await asyncio.sleep(120)
            zone = _zone(await self.ha.time_zone())
            if zone is None:
                continue
            if str(zone) != str(self.tz):
                _LOGGER.info("Time zone is now %s (was %s)", zone, self.tz)
                self.tz = zone
                self.relocalize()
                self.stop_loops()
                self.start_loops()
            self.tz_source = "home_assistant"
            return

    def recompute_targets(self) -> None:
        time_is_end = self.opts.program_time_marks == "end"

        def target(programs: list[dict[str, Any]], minute: int) -> float | None:
            program = active_program(programs, minute, time_is_end)
            return program.get("soc") if program else None

        count = self.db.recompute_targets(target)
        _LOGGER.info("Recomputed program SOC for %d readings (program time = %s)", count, self.opts.program_time_marks)

    def apply_settings(self, opts: Options) -> None:
        """Use new settings right away: new Claude client, new schedule and recording interval."""
        marks_changed = opts.program_time_marks != self.opts.program_time_marks
        weekend_changed = opts.weekend_days != self.opts.weekend_days
        self.opts = opts
        if weekend_changed:
            self.relocalize()
        elif marks_changed:
            self.recompute_targets()
        self._client = self._make_client(opts.claude_api_key)
        self.stop_loops()
        self.start_loops()
        _LOGGER.info("Settings updated")

    async def record(self) -> dict[str, Any] | None:
        snapshot = await collect(self.ha, self.opts, self.tz)
        self.last_snapshot = snapshot
        if not has_data(snapshot):
            _LOGGER.warning("No sensor values available; reading not stored")
            return None
        snapshot["control_mode"] = self.control["mode"]
        self.db.add_reading(snapshot, self.tz)
        self._check_outage_change(snapshot)
        return snapshot

    def _check_outage_change(self, snapshot: dict[str, Any]) -> None:
        """A new or changed outage announcement gets its own prediction (rate limited)."""
        if not self.opts.outages_sensor or snapshot["outages_state"] is None:
            return
        key = json.dumps([snapshot["outages_state"], snapshot["outages_attrs"]], sort_keys=True, default=str)
        previous, self._last_outage_key = self._last_outage_key, key
        if previous is None or previous == key or not self.can_predict or self.analysis_running:
            return
        if time.time() - self._last_outage_run < OUTAGE_RERUN_SECONDS:
            return
        self._last_outage_run = time.time()
        _LOGGER.info("Outage information changed; running an extra prediction")
        self.spawn(self.run_analysis("outage"))

    async def notify(self, title: str, message: str | None, enabled: bool = True, kind: str = "info") -> None:
        if enabled and message and self.opts.notify_services:
            await notify.send(self.ha, self.opts, title, message, kind=kind, url=self.panel_url)

    async def notify_actions(self, actions: list[dict[str, Any]], reason: str, intro: str | None = None) -> None:
        await self.notify(
            f"🔋 BatteryAI: {reason}", notify.actions_message(actions, intro), self.opts.notify_soc_changes, "changes"
        )

    async def compress_old(self) -> dict[str, int]:
        cutoff = int(time.time()) - self.opts.detail_days * 86400
        result = await asyncio.to_thread(self.db.compress_before, cutoff)
        if result["removed"]:
            _LOGGER.info(
                "Compressed %d readings older than %d days into %d hourly rows",
                result["removed"], self.opts.detail_days, result["added"],
            )
        self._last_compress = time.time()
        return result

    async def recorder_loop(self) -> None:
        interval = self.opts.record_interval_minutes * 60
        while True:
            snapshot = None
            try:
                snapshot = await self.record()
            except Exception:  # keep recording even if one cycle fails
                _LOGGER.exception("Recording failed")
            try:
                await asyncio.to_thread(self.bill.record_appliances, self.opts, self.tz, snapshot)
                await self.bill.record(self.ha, self.opts, self.tz)
            except Exception:
                _LOGGER.exception("Recording the monthly bill failed")
            if time.time() - self._last_compress > 6 * 3600:
                try:
                    await self.compress_old()
                except Exception:
                    _LOGGER.exception("Compressing old readings failed")
            await asyncio.sleep(interval - time.time() % interval)

    async def scheduler_loop(self) -> None:
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in self.opts.analysis_times())
        _LOGGER.info("Scheduled analyses at %s (%s)", times, self.tz)
        while True:
            self.next_analysis = self.opts.next_analysis(datetime.now(self.tz), self.tz)
            while (remaining := (self.next_analysis - datetime.now(self.tz)).total_seconds()) > 0:
                await asyncio.sleep(min(remaining, 60))
            try:
                # Shielded so that saving settings (which restarts this loop) can't cut a run short.
                await asyncio.shield(self.run_analysis("schedule"))
            except Exception:
                _LOGGER.exception("Scheduled analysis failed")

    @property
    def can_predict(self) -> bool:
        engine = self.opts.prediction_engine
        if engine == "claude":
            return self._client is not None
        if engine == "openai":
            return bool(self.opts.openai_api_key)
        return True

    @property
    def engine_label(self) -> str:
        return {
            "claude": self.opts.claude_model,
            "openai": self.opts.openai_model,
            "local_fast": "local-fast (statistics + rules)",
            "local_llm": f"local {local_llm.MODEL_NAME}",
        }[self.opts.prediction_engine]

    async def _predict(self, snapshot: dict[str, Any], trigger: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Runs the selected engine; returns (outcome, input data stored with the analysis)."""
        engine = self.opts.prediction_engine
        if engine == "claude":
            if self._client is None:
                raise AnalysisError("Set the Claude API key in the Settings tab.")
            data = build_input(self.db, self.opts, snapshot, self.tz, trigger)
            return await analyze(self._client, self.opts, data), data
        if engine == "openai":
            if not self.opts.openai_api_key:
                raise AnalysisError("Set the OpenAI API key in the Settings tab.")
            data = build_input(self.db, self.opts, snapshot, self.tz, trigger)
            return await openai_engine.analyze(self.opts, data), data

        baseline = await asyncio.to_thread(local_fast.analyze, self.db, self.opts, snapshot, self.tz)
        if engine == "local_fast":
            return {"result": baseline, "model": self.engine_label, "input_tokens": None, "output_tokens": None}, {
                "engine": engine, "snapshot": snapshot,
            }
        try:
            answer = await local_llm.run(self.opts, snapshot, baseline)
        except (RuntimeError, ValueError) as err:
            raise AnalysisError(str(err)) from err
        result = local_llm.merge(self.opts, baseline, answer["result"])
        usage = answer.get("usage") or {}
        return {
            "result": result,
            "model": self.engine_label,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }, {"engine": engine, "prompt": local_llm.build_prompt(self.opts, snapshot, baseline)}

    @property
    def analysis_running(self) -> bool:
        return self._analysis_lock.locked()

    async def run_analysis(self, trigger: str) -> int:
        async with self._analysis_lock:
            analysis_id = self.db.start_analysis(trigger, self.engine_label)
            _LOGGER.info("Starting analysis #%d (%s)", analysis_id, trigger)
            data: dict[str, Any] | None = None
            try:
                # record() may itself trigger an outage run; that waits for this lock.
                snapshot = await self.record() or self.last_snapshot
                if snapshot is None or not has_data(snapshot):
                    raise AnalysisError("No sensor data available from Home Assistant.")
                outcome, data = await self._predict(snapshot, trigger)
            except AnalysisError as err:
                _LOGGER.error("Analysis #%d failed: %s", analysis_id, err)
                self.db.finish_analysis(analysis_id, status="error", error=str(err), input_data=data)
                await self.notify("❌ BatteryAI: prediction failed", str(err), self.opts.notify_errors, "error")
            except Exception as err:
                _LOGGER.exception("Analysis #%d crashed", analysis_id)
                self.db.finish_analysis(analysis_id, status="error", error=f"Unexpected error: {err}", input_data=data)
                await self.notify("❌ BatteryAI: prediction failed", f"Unexpected error: {err}", self.opts.notify_errors, "error")
            else:
                result = outcome["result"]
                self.db.finish_analysis(
                    analysis_id,
                    status="ok",
                    model=outcome["model"],
                    summary=result.get("summary"),
                    result=result,
                    input_data=data,
                    input_tokens=outcome["input_tokens"],
                    output_tokens=outcome["output_tokens"],
                )
                _LOGGER.info("Analysis #%d done: %s", analysis_id, result.get("summary"))
                actions: list[dict[str, Any]] | None = None
                if self.control["mode"] == "auto":
                    try:
                        actions = await control.apply_prediction(self.ha, self.opts, result)
                        self.db.add_actions(analysis_id, actions)
                    except Exception as err:
                        _LOGGER.exception("Applying analysis #%d failed", analysis_id)
                        actions = [{"status": "error", "error": f"applying the plan failed: {err}"}]
                # One notification per run: the plan and what was written to the inverter.
                if self.opts.notify_predictions:
                    title, message = notify.plan_notification(
                        result, self.opts, trigger=trigger, actions=actions,
                        current_programs=snapshot.get("deye_programs"),
                    )
                    await self.notify(title, message, kind="plan")
                elif actions:
                    await self.notify_actions(actions, "SOC updated")
            return analysis_id


# HTTP API -------------------------------------------------------------------

routes = web.RouteTableDef()


class MemoryLogHandler(logging.Handler):
    """Keeps the most recent log records for the Logs tab."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.counter = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.counter += 1
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        self.records.append(
            {"id": self.counter, "ts": record.created, "level": record.levelname, "logger": record.name, "message": message}
        )


LOG_BUFFER = MemoryLogHandler()


def addon_version() -> str:
    """The add-on version: from the build (BUILD_VERSION), else from the add-on's config.yaml."""
    version = os.environ.get("BATTERYAI_VERSION", "").strip()
    if version and version != "dev":
        return version
    for path in (Path(__file__).parent / "addon_config.yaml", Path(__file__).parent.parent / "config.yaml"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
        except OSError:
            continue
    return "dev"


def _app(request: web.Request) -> BatteryAI:
    return request.app["batteryai"]


def _int_param(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(request.query.get(name, default))))
    except ValueError:
        return default


def _asset_version() -> str:
    """Changes whenever a UI file changes, so browsers fetch new files after an update."""
    digest = hashlib.sha1()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@routes.get("/")
async def index(_: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("static/style.css", "static/app.js", "static/settings.js", "static/vendor/chart.umd.js", "static/logo.svg"):
        html = html.replace(f'"{asset}"', f'"{asset}?v={ASSET_VERSION}"')
    return web.Response(text=html, content_type="text/html", headers=NO_CACHE)


@web.middleware
async def revalidate_static(request: web.Request, handler: Any) -> web.StreamResponse:
    """Make browsers check UI files with the add-on (cheap 304s) instead of using stale copies."""
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers.update(NO_CACHE)
    return response


@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    app = _app(request)
    opts = app.opts
    latest = app.db.latest_reading()
    warnings = []
    if app.tz_source == "fallback":
        warnings.append(
            "Home Assistant has not reported its time zone yet, so times are in UTC "
            "(tariff, schedule and programs may be off). Retrying every 2 minutes."
        )
    if opts.prediction_engine == "claude" and not opts.claude_api_key:
        warnings.append("Claude API key is not set.")
    if opts.prediction_engine == "openai" and not opts.openai_api_key:
        warnings.append("OpenAI API key is not set.")
    if opts.prediction_engine == "local_llm":
        llm = local_llm.available()
        if not llm["runtime"]:
            warnings.append("The local LLM runtime is missing from this add-on build; predictions will fail.")
        elif not llm["model"]:
            warnings.append("The local model is not downloaded yet (Settings → Prediction engine).")
    if app.ha.last_error:
        warnings.append(app.ha.last_error)
    elif app.last_snapshot and app.last_snapshot["missing_entities"]:
        warnings.append(
            "Unavailable entities: " + ", ".join(app.last_snapshot["missing_entities"])
            + ". Use the Test buttons in Settings to see why."
        )
    unset = [key for key, entity in opts.sensor_map().items() if not entity and key in REQUIRED_SENSORS]
    unset += [f"Deye program {p.slot}" for p in opts.deye_programs if not (p.time_entity and p.soc_entity)]
    if unset:
        warnings.append("Not configured yet: " + ", ".join(k.replace("_", " ") for k in unset) + ".")
    return web.json_response(
        {
            "version": addon_version(),
            "time_zone": str(app.tz),
            "time_zone_source": app.tz_source,
            "model": app.engine_label,
            "engine": opts.prediction_engine,
            "analysis_times": [f"{h:02d}:{m:02d}" for h, m in opts.analysis_times()],
            "next_analysis": app.next_analysis.isoformat(timespec="minutes") if app.next_analysis else None,
            "analysis_running": app.analysis_running,
            "record_interval_minutes": opts.record_interval_minutes,
            "reading_count": app.db.reading_count(),
            "latest": latest,
            "battery_name": opts.battery_name,
            "solar_forecast_percent": opts.solar_forecast_percent,
            "forecast_raw": app.last_snapshot.get("forecast_raw") if app.last_snapshot else None,
            "units": app.last_snapshot["units"] if app.last_snapshot else {},
            "active_program_slot": app.last_snapshot["active_program_slot"] if app.last_snapshot else None,
            "program_time_marks": opts.program_time_marks,
            "programs_configured": {
                p.slot: {"soc": bool(p.soc_entity), "charge": bool(p.charge_entity)} for p in opts.deye_programs
            },
            "sensors": opts.sensor_map(),
            "tariff": {
                **opts.tariff_dict(),
                "now": opts.tariff_at(datetime.now(app.tz).hour * 60 + datetime.now(app.tz).minute)[1],
                "now_price": opts.tariff_at(datetime.now(app.tz).hour * 60 + datetime.now(app.tz).minute)[0],
            },
            "appliances": [
                {"id": a.id, "name": a.name, "temperature_dependent": a.temperature_dependent}
                for a in opts.appliances
                if a.entity
            ],
            "weather": app.last_snapshot.get("weather") if app.last_snapshot else None,
            "control": {
                "mode": app.control["mode"],
                "since": app.control["since"],
                "charge_all_soc": opts.charge_all_soc_percent,
                "can_write": any(p.soc_entity for p in opts.deye_programs),
                "precharge_enabled": app.control.get("precharge_enabled", True),
                "precharge": app.control.get("precharge"),
                "precharge_minutes": opts.outage_precharge_minutes,
                "precharge_soc": opts.outage_precharge_soc_percent,
                "precharge_smart": app.control.get("precharge_smart", True),
                "emergency_enabled": app.control.get("emergency_enabled", True),
                "precharge_smart_applies": not opts.single_price and bool(opts.outage_duration_sensor),
                "outage_plan": app.outage_plan,
            },
            "outage_minutes": {
                "entity": opts.outage_minutes_sensor,
                "minutes": app.outage_minutes,
                "ts": app.outage_minutes_ts,
                "duration_entity": opts.outage_duration_sensor,
                "duration": app.outage_duration,
                "emergency_entity": opts.emergency_outage_sensor,
                "emergency": app.emergency,
            },
            "history_import": app.history_import,
            "warnings": warnings,
        }
    )


@routes.get("/api/readings")
async def readings(request: web.Request) -> web.Response:
    hours = _int_param(request, "hours", 48, 1, 24 * 90)
    rows = _app(request).db.readings_since(int(time.time()) - hours * 3600)
    step = max(1, len(rows) // MAX_CHART_POINTS)
    keys = ("ts", "target_soc", *READING_FIELDS)
    return web.json_response([{k: row[k] for k in keys} for row in rows[::step]])


@routes.get("/api/daily")
async def daily(request: web.Request) -> web.Response:
    app = _app(request)
    days = _int_param(request, "days", 14, 1, 90)
    return web.json_response(app.db.daily_summary(days, datetime.now(app.tz).date()))


@routes.get("/api/analyses")
async def analyses(request: web.Request) -> web.Response:
    limit = _int_param(request, "limit", 30, 1, 200)
    return web.json_response(_app(request).db.analyses(limit))


@routes.get(r"/api/analyses/{analysis_id:\d+}/input")
async def analysis_input(request: web.Request) -> web.Response:
    data = _app(request).db.analysis_input(int(request.match_info["analysis_id"]))
    if data is None:
        raise web.HTTPNotFound()
    return web.json_response(data)


@routes.get("/api/accuracy")
async def accuracy(request: web.Request) -> web.Response:
    app = _app(request)
    days = _int_param(request, "days", 14, 1, 90)
    return web.json_response(accuracy_report(app.db, days, datetime.now(app.tz).date(), app.tz))


@routes.get("/api/economy")
async def economy(request: web.Request) -> web.Response:
    app = _app(request)
    days = _int_param(request, "days", 30, 1, 365)
    report = economy_report(app.db, days, datetime.now(app.tz).date(), app.opts.tariff_at)
    report["currency"] = app.opts.tariff_currency
    report["has_grid_sensor"] = bool(app.opts.grid_import_sensor)
    report["has_pv_power_sensor"] = bool(app.opts.pv_power_sensor)
    return web.json_response(report)


@routes.get("/api/logs")
async def logs(request: web.Request) -> web.Response:
    """Recent log lines; ?after=<id> returns only newer ones, ?level=WARNING filters."""
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    minimum = levels.get(request.query.get("level", "INFO").upper(), 20)
    after = _int_param(request, "after", 0, 0, 1 << 62)
    entries = [r for r in LOG_BUFFER.records if r["id"] > after and levels.get(r["level"], 0) >= minimum]
    return web.json_response(entries[-1000:])


def _dir_size(path: Path) -> tuple[int, int]:
    total = files = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
                files += 1
        except OSError:
            pass
    return total, files


@routes.get("/api/storage")
async def storage(request: web.Request) -> web.Response:
    app = _app(request)
    data_dir = Path(DATA_DIR)

    def collect() -> dict[str, Any]:
        files = []
        for item in sorted(data_dir.iterdir()) if data_dir.exists() else []:
            if item.is_file():
                files.append({"name": item.name, "bytes": item.stat().st_size})
        data_total, data_files = _dir_size(data_dir)
        app_total, _ = _dir_size(Path(__file__).parent)
        disk = shutil.disk_usage(data_dir)
        return {
            "database": app.db.stats(),
            "data_dir": str(data_dir),
            "data_bytes": data_total,
            "data_files": data_files,
            "files": files,
            "app_bytes": app_total,
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        }

    return web.json_response(await asyncio.to_thread(collect))


@routes.post("/api/storage/vacuum")
async def vacuum(request: web.Request) -> web.Response:
    app = _app(request)
    before = app.db.stats()["file_bytes"]
    await asyncio.to_thread(app.db.vacuum)
    after = app.db.stats()["file_bytes"]
    _LOGGER.info("Database compacted: %d -> %d bytes", before, after)
    return web.json_response({"before": before, "after": after})


@routes.post("/api/storage/compress")
async def compress(request: web.Request) -> web.Response:
    return web.json_response(await _app(request).compress_old())


@routes.post(r"/api/programs/{slot:\d}")
async def set_program(request: web.Request) -> web.Response:
    """Manual change from the Battery control card: {"soc": 60} and/or {"grid_charge": true}."""
    app = _app(request)
    slot = int(request.match_info["slot"])
    program = next((p for p in app.opts.deye_programs if p.slot == slot), None)
    if program is None:
        raise web.HTTPNotFound()
    body = await request.json()
    actions: list[dict[str, Any]] = []
    if "soc" in body:
        try:
            soc = float(body["soc"])
        except (TypeError, ValueError):
            return web.json_response({"error": "SOC must be a number"}, status=400)
        if not 0 <= soc <= 100:
            return web.json_response({"error": "SOC must be between 0 and 100"}, status=400)
        if not program.soc_entity:
            return web.json_response({"error": f"Program {slot} has no SOC entity in Settings."}, status=400)
        action: dict[str, Any] = {"slot": slot, "entity_id": program.soc_entity, "time": time.time(), "manual": True}
        try:
            action.update(status="set", **await control.set_soc(app.ha, program.soc_entity, soc))
        except HAError as err:
            action.update(status="error", error=str(err))
        actions.append(action)
    if "grid_charge" in body:
        if not program.charge_entity:
            return web.json_response({"error": f"Program {slot} has no grid charge switch in Settings."}, status=400)
        action = {"slot": slot, "entity_id": program.charge_entity, "kind": "grid_charge", "time": time.time(), "manual": True}
        try:
            action.update(status="set", **await control.set_switch(app.ha, program.charge_entity, bool(body["grid_charge"])))
        except HAError as err:
            action.update(status="error", error=str(err))
        actions.append(action)
    _LOGGER.info("Manual program change: %s", actions)
    await app.notify_actions(actions, "program changed manually")
    await asyncio.sleep(1)  # let the inverter integration report the new state
    await app.record()
    return web.json_response({"actions": actions})


@routes.get("/api/monthly")
async def monthly(request: web.Request) -> web.Response:
    app = _app(request)
    rows = await asyncio.to_thread(monthly_summary, app.db, datetime.now(app.tz).date(), app.opts.tariff_at)
    return web.json_response({"months": rows, "currency": app.opts.tariff_currency})


@routes.get("/api/bill")
async def bill_view(request: web.Request) -> web.Response:
    app = _app(request)
    today = datetime.now(app.tz).date()
    year = _int_param(request, "year", today.year, 2000, 2100)
    report = await asyncio.to_thread(bill_report, app.db, year, today, app.opts)
    report["backfilling"] = app.bill.backfilling
    return web.json_response(report)


@routes.post("/api/control")
async def set_control(request: web.Request) -> web.Response:
    """Body {"mode": "off" | "auto"}; Charge all has its own endpoint."""
    mode = (await request.json()).get("mode")
    if mode not in ("off", "auto"):
        return web.json_response({"error": "mode must be off or auto"}, status=400)
    actions = await _app(request).set_mode(mode)
    return web.json_response({"mode": mode, "actions": actions})


@routes.post("/api/control/charge_all")
async def charge_all(request: web.Request) -> web.Response:
    app = _app(request)
    if not any(p.soc_entity for p in app.opts.deye_programs):
        return web.json_response({"error": "No Deye program SOC entities configured."}, status=400)
    actions = await app.charge_all()
    return web.json_response({"mode": "charge_all", "actions": actions})


@routes.post("/api/bill/reprice")
async def bill_reprice(request: web.Request) -> web.Response:
    """Body {"month": "2026-10"}: recalculate that month with the current tariffs."""
    app = _app(request)
    month = str((await request.json()).get("month", ""))
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return web.json_response({"error": "month must look like 2026-10"}, status=400)
    result = await asyncio.to_thread(reprice_month, app.db, month, app.opts)
    _LOGGER.info("Monthly bill %s recalculated with the current tariffs: %s", month, result)
    return web.json_response(result)


@routes.post("/api/bill/price")
async def bill_price(request: web.Request) -> web.Response:
    """Body {"month": "2026-10", "tariff": "Peak", "price": 4.32}: that month's price per kWh."""
    app = _app(request)
    body = await request.json()
    month, tariff = str(body.get("month", "")), str(body.get("tariff", ""))
    if not re.fullmatch(r"\d{4}-\d{2}", month) or not tariff:
        return web.json_response({"error": "month (2026-10) and tariff are needed"}, status=400)
    try:
        price = float(str(body.get("price")).replace(",", "."))
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return web.json_response({"error": "enter a price per kWh (0 or more)"}, status=400)
    await asyncio.to_thread(app.db.set_bill_price, month, tariff, price)
    _LOGGER.info("Monthly bill %s: %s price set to %s", month, tariff, price)
    return web.json_response({"month": month, "tariff": tariff, "price": price})


@routes.get("/api/backup")
async def download_backup(request: web.Request) -> web.StreamResponse:
    """A zip of the database, settings and control state."""
    app = _app(request)
    path = await asyncio.to_thread(backup.create_backup, app.db, addon_version())
    try:
        response = web.StreamResponse(headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "Content-Length": str(path.stat().st_size),
        })
        await response.prepare(request)
        with open(path, "rb") as fh:
            while chunk := await asyncio.to_thread(fh.read, 1 << 20):
                await response.write(chunk)
        await response.write_eof()
        _LOGGER.info("Backup downloaded: %s (%d bytes)", path.name, path.stat().st_size)
        return response
    finally:
        path.unlink(missing_ok=True)


@routes.post("/api/backup/restore")
async def restore_backup(request: web.Request) -> web.Response:
    """The request body is a backup zip; it replaces the database and settings."""
    app = _app(request)
    if app.analysis_running or app.history_import["running"] or app.bill.backfilling:
        return web.json_response({"error": "A prediction or history import is running; try again when it has finished."}, status=409)
    backup.WORK_DIR.mkdir(parents=True, exist_ok=True)
    upload = backup.WORK_DIR / "upload.zip"
    size = 0
    try:
        with open(upload, "wb") as fh:
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                await asyncio.to_thread(fh.write, chunk)
        if not size:
            return web.json_response({"error": "No file received."}, status=400)
        try:
            manifest = await asyncio.to_thread(backup.check_backup, upload)
        except backup.BackupError as err:
            return web.json_response({"error": str(err)}, status=400)
    finally:
        upload.unlink(missing_ok=True)
    async with app._analysis_lock:
        await app.restore()
    return web.json_response({"ok": True, "manifest": manifest})


@routes.post("/api/battery_name")
async def set_battery_name(request: web.Request) -> web.Response:
    """Body {"name": "..."}: the battery bank's name in the header (edited in place)."""
    app = _app(request)
    name = " ".join(str((await request.json()).get("name") or "").split())[:40]
    app.opts.battery_name = name
    save_settings(app.opts)
    return web.json_response({"name": name})


@routes.post("/api/control/precharge")
async def set_precharge(request: web.Request) -> web.Response:
    """Body {"enabled": bool}: charge before an outage on/off; off also ends a running one."""
    app = _app(request)
    body = await request.json()
    if "emergency" in body:
        app.control["emergency_enabled"] = bool(body["emergency"])
        control.save_state(app.control)
        _LOGGER.info("Charge on emergency outages %s", "on" if body["emergency"] else "off")
        async with app._outage_lock:
            running = app.control.get("precharge")
            if not body["emergency"] and running and running.get("emergency"):
                await app.end_precharge(running)
        if body["emergency"]:
            await app.check_outage()
        return web.json_response({"emergency": app.control["emergency_enabled"]})
    if "smart" in body:
        app.control["precharge_smart"] = bool(body["smart"])
        control.save_state(app.control)
        _LOGGER.info("Tariff-aware charge before outages %s", "on" if body["smart"] else "off")
        await app.check_outage()
        return web.json_response({"smart": app.control["precharge_smart"]})
    enabled = bool(body.get("enabled"))
    app.control["precharge_enabled"] = enabled
    control.save_state(app.control)
    _LOGGER.info("Charge before outages %s", "on" if enabled else "off")
    async with app._outage_lock:
        if not enabled and app.control.get("precharge"):
            await app.end_precharge(app.control["precharge"])
    if enabled:
        await app.check_outage()
    return web.json_response({"enabled": enabled})


@routes.post(r"/api/analyses/{analysis_id:\d+}/apply")
async def apply_analysis(request: web.Request) -> web.Response:
    app = _app(request)
    analysis = app.db.analysis(int(request.match_info["analysis_id"]))
    if not analysis or analysis["status"] != "ok":
        return web.json_response({"error": "No successful prediction with this id."}, status=404)
    actions = await control.apply_prediction(app.ha, app.opts, analysis["result"])
    app.db.add_actions(analysis["id"], [{**a, "manual": True} for a in actions])
    await app.notify_actions(actions, "SOC updated (manual)")
    return web.json_response({"actions": actions})


@routes.post("/api/history/import")
async def history_import(request: web.Request) -> web.Response:
    app = _app(request)
    body = await request.json() if request.can_read_body else {}
    try:
        days = max(1, min(365, int(body.get("days") or app.opts.history_days)))
    except (TypeError, ValueError):
        return web.json_response({"error": "days must be a number"}, status=400)
    if not app.start_history_import(days):
        return web.json_response({"error": "An import is already running."}, status=409)
    return web.json_response({"started": True, "days": days}, status=202)


@routes.post("/api/analyze")
async def analyze_now(request: web.Request) -> web.Response:
    app = _app(request)
    if app.analysis_running:
        return web.json_response({"error": "An analysis is already running."}, status=409)
    request.app["background"].add(task := asyncio.create_task(app.run_analysis("manual")))
    task.add_done_callback(request.app["background"].discard)
    return web.json_response({"started": True}, status=202)


# Settings and connection tests --------------------------------------------------


@routes.get("/api/settings")
async def get_settings(request: web.Request) -> web.Response:
    return web.json_response(_app(request).opts.public_dict())


@routes.put("/api/settings")
async def put_settings(request: web.Request) -> web.Response:
    app = _app(request)
    try:
        raw = await request.json()
        opts = parse_settings(raw, app.opts)
    except SettingsError as err:
        return web.json_response({"errors": err.errors}, status=400)
    except ValueError:
        return web.json_response({"errors": {"_": "Invalid JSON"}}, status=400)
    save_settings(opts)
    app.apply_settings(opts)
    return web.json_response(opts.public_dict())


@routes.get("/api/test/ha")
async def test_ha(request: web.Request) -> web.Response:
    ha = _app(request).ha
    info: dict[str, Any] = {"url": ha.base_url, "token_source": ha.token_source}
    try:
        await ha.request("/")
        config = await ha.config()
    except HAError as err:
        return web.json_response({**info, "ok": False, "error": str(err), "kind": err.kind})
    return web.json_response(
        {
            **info,
            "ok": True,
            "version": config.get("version"),
            "location_name": config.get("location_name"),
            "time_zone": config.get("time_zone"),
        }
    )


@routes.get("/api/test/entity")
async def test_entity(request: web.Request) -> web.Response:
    """Reads one entity. kind=numeric|time|text says what value the setting expects."""
    entity_id = request.query.get("entity_id", "").strip()
    kind = request.query.get("kind", "text")
    try:
        state = await _app(request).ha.fetch_state(entity_id)
    except HAError as err:
        return web.json_response({"ok": False, "entity_id": entity_id, "error": str(err), "kind": err.kind})

    value = state.get("state")
    attributes = state.get("attributes") or {}
    warning = None
    if value is None or str(value).strip().lower() in UNAVAILABLE:
        warning = (
            f"The entity exists, but its state is '{value}'. The integration that provides it "
            "is not delivering data right now."
        )
    elif kind == "numeric" and to_float(value) is None:
        warning = "Expected a number, but the state is not numeric."
    elif kind == "time" and parse_hhmm(value) is None:
        warning = "Expected a time such as 01:00, 01:00:00 or 100."
    elif kind == "switch" and str(value) not in ("on", "off"):
        warning = "Expected a switch (on/off) that turns grid charging of this program on or off."
    detail = None
    if entity_id.startswith("weather."):
        weather = await weather_details(_app(request).ha, entity_id, _app(request).tz)
        unit = weather.get("unit") or ""
        tomorrow = weather.get("tomorrow")
        detail = f"now {weather.get('outdoor_temp')} {unit}".strip()
        if tomorrow:
            detail += (
                f" · tomorrow {tomorrow.get('templow', '?')}…{tomorrow.get('temperature', '?')} {unit}, "
                f"{tomorrow.get('condition', '')}"
            )
        elif weather.get("forecast_error"):
            warning = f"No forecast: {weather['forecast_error']}"
        else:
            warning = "This weather entity has no forecast for tomorrow."
    return web.json_response(
        {
            "ok": warning is None,
            "detail": detail,
            "entity_id": state.get("entity_id", entity_id),
            "state": value,
            "unit": attributes.get("unit_of_measurement"),
            "friendly_name": attributes.get("friendly_name"),
            "last_updated": state.get("last_updated"),
            "warning": warning,
        }
    )


@routes.get("/api/entities")
async def entities(request: web.Request) -> web.Response:
    try:
        states = await _app(request).ha.states()
    except HAError as err:
        return web.json_response({"error": str(err)}, status=502)
    return web.json_response(
        sorted(
            (
                {
                    "entity_id": s["entity_id"],
                    "name": (s.get("attributes") or {}).get("friendly_name") or "",
                    "state": s.get("state"),
                    "unit": (s.get("attributes") or {}).get("unit_of_measurement") or "",
                }
                for s in states
                if s.get("entity_id")
            ),
            key=lambda e: e["entity_id"],
        )
    )


@routes.get("/api/notify_services")
async def notify_services(request: web.Request) -> web.Response:
    """Notify services in Home Assistant; phones appear as mobile_app_<device>."""
    try:
        domains = await _app(request).ha.request("/services")
    except HAError as err:
        return web.json_response({"error": str(err)}, status=502)
    services = next((d.get("services") or {} for d in domains if d.get("domain") == "notify"), {})
    return web.json_response(
        sorted(
            ({"service": name, "phone": name.startswith("mobile_app_"), "name": (info or {}).get("name") or ""}
             for name, info in services.items()),
            key=lambda s: (not s["phone"], s["service"]),
        )
    )


@routes.post("/api/test/notify")
async def test_notify(request: web.Request) -> web.Response:
    app = _app(request)
    body = await request.json() if request.can_read_body else {}
    service = str(body.get("service") or "").strip().removeprefix("notify.")
    if not service:
        return web.json_response({"ok": False, "error": "Enter a notify service first."})
    try:
        test_opts = dataclasses.replace(app.opts, notify_services=[service])
        if "prefix" in body:  # the value in the form, saved or not
            test_opts.notify_prefix = str(body.get("prefix") or "").strip()[:40]
        errors = await notify.send(
            app.ha, test_opts, "🔋 BatteryAI: test",
            "Test notification from BatteryAI ✓\nThe first lines are the brief. Pull this notification down "
            "(or long-press it) to read everything, and use “Open BatteryAI” to open the panel."
            + ("" if app.panel_url else "\n(No panel link: the add-on could not ask the Supervisor for it.)"),
            kind="test", url=app.panel_url,
        )
        if errors:
            raise HAError("http", errors[0].split(": ", 1)[-1])
    except HAError as err:
        return web.json_response({"ok": False, "error": str(err)})
    return web.json_response({"ok": True})


FALLBACK_MODEL_LIST = [
    {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5"},
    {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1"},
    {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
]


@routes.get("/api/models")
async def models(request: web.Request) -> web.Response:
    """Models the saved API key can use (Models API), newest first; a built-in list without a key."""
    app = _app(request)
    current = app.opts.claude_model
    found: list[dict[str, str]] = []
    error = None
    if app.opts.claude_api_key:
        client = anthropic.AsyncAnthropic(api_key=app.opts.claude_api_key, max_retries=1, timeout=20.0)
        try:
            async for model in client.models.list(limit=100):
                found.append({"id": model.id, "display_name": model.display_name})
        except anthropic.APIError as err:
            error = str(err)
        finally:
            await client.close()
    models_list = found or FALLBACK_MODEL_LIST
    if current and current not in {m["id"] for m in models_list}:
        models_list = [{"id": current, "display_name": current}, *models_list]

    openai_current = app.opts.openai_model
    openai_found: list[dict[str, str]] = []
    openai_error = None
    if app.opts.openai_api_key:
        try:
            openai_found = await openai_engine.list_models(app.opts.openai_api_key)
        except AnalysisError as err:
            openai_error = str(err)
    openai_list = openai_found or [{"id": m, "display_name": m} for m in openai_engine.FALLBACK_MODELS]
    if openai_current and openai_current not in {m["id"] for m in openai_list}:
        openai_list = [{"id": openai_current, "display_name": openai_current}, *openai_list]
    return web.json_response({
        "models": models_list,
        "current": current,
        "live": bool(found),
        "error": error,
        "openai_models": openai_list,
        "openai_current": openai_current,
        "openai_live": bool(openai_found),
        "openai_error": openai_error,
        "engine": app.opts.prediction_engine,
        "engines": [
            {"id": "claude", "name": "Claude (cloud)"},
            {"id": "openai", "name": "ChatGPT (OpenAI cloud)"},
            {"id": "local_fast", "name": "Local fast – statistics + rules (light CPU)"},
            {"id": "local_llm", "name": f"Local slow – {local_llm.MODEL_NAME} LLM (heavy CPU)"},
        ],
    })


@routes.get("/api/local_llm")
async def local_llm_status(request: web.Request) -> web.Response:
    app = _app(request)
    return web.json_response({
        **local_llm.available(),
        "download": app.model_download.state,
        "url": local_llm.MODEL_URL,
        "cpu_count": os.cpu_count(),
    })


@routes.post("/api/local_llm/download")
async def local_llm_download(request: web.Request) -> web.Response:
    started = _app(request).model_download.start()
    return web.json_response({"started": started}, status=202 if started else 409)


@routes.post("/api/local_llm/cancel")
async def local_llm_cancel(request: web.Request) -> web.Response:
    _app(request).model_download.cancel()
    return web.json_response({"cancelled": True})


@routes.delete("/api/local_llm/model")
async def local_llm_delete(request: web.Request) -> web.Response:
    app = _app(request)
    if app.model_download.state["running"]:
        return web.json_response({"error": "A download is running."}, status=409)
    if app.opts.prediction_engine == "local_llm" and app.analysis_running:
        return web.json_response({"error": "A prediction is using the model."}, status=409)
    local_llm.delete_model()
    return web.json_response({"deleted": True})


@routes.get("/api/predicted_load")
async def predicted_load(request: web.Request) -> web.Response:
    """Hourly load forecasts as points: each run predicts the day after it ran (later runs win)."""
    app = _app(request)
    hours = _int_param(request, "hours", 48, 1, 24 * 90)
    since = int(time.time()) - hours * 3600
    points: dict[int, dict[str, Any]] = {}
    for analysis in app.db.ok_analyses_since(since - 2 * 86400):
        day = datetime.fromtimestamp(analysis["ts"], app.tz).date() + timedelta(days=1)
        for item in analysis["result"].get("hourly_forecast_tomorrow") or []:
            try:
                hour = int(item["hour"])
                ts = int(datetime(day.year, day.month, day.day, hour, tzinfo=app.tz).timestamp())
            except (KeyError, TypeError, ValueError):
                continue
            if ts >= since:
                points[ts] = {"ts": ts, "load_w": item.get("load_w")}
    return web.json_response([points[ts] for ts in sorted(points)])


@routes.post("/api/test/claude")
async def test_claude(request: web.Request) -> web.Response:
    """Checks the API key and model with the Models API (no tokens are used)."""
    app = _app(request)
    body = await request.json() if request.can_read_body else {}
    api_key = (body.get("api_key") or "").strip() or app.opts.claude_api_key
    model = (body.get("model") or "").strip() or app.opts.claude_model
    if not api_key:
        return web.json_response({"ok": False, "error": "No API key entered."})
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=1, timeout=20.0)
    try:
        info = await client.models.retrieve(model)
    except anthropic.AuthenticationError:
        return web.json_response({"ok": False, "error": "Claude rejected the API key."})
    except anthropic.PermissionDeniedError as err:
        return web.json_response({"ok": False, "error": f"The key has no access: {err.message}"})
    except anthropic.NotFoundError:
        return web.json_response({"ok": False, "error": f"The key works, but model '{model}' was not found."})
    except anthropic.APIStatusError as err:
        return web.json_response({"ok": False, "error": f"Claude API error {err.status_code}: {err.message}"})
    except anthropic.APIConnectionError as err:
        return web.json_response({"ok": False, "error": f"Could not reach the Claude API: {err}"})
    finally:
        await client.close()
    return web.json_response({"ok": True, "model": info.id, "display_name": info.display_name})


@routes.post("/api/test/openai")
async def test_openai(request: web.Request) -> web.Response:
    """Checks the OpenAI API key and model with the Models API (no tokens are used)."""
    app = _app(request)
    body = await request.json() if request.can_read_body else {}
    api_key = (body.get("api_key") or "").strip() or app.opts.openai_api_key
    model = (body.get("model") or "").strip() or app.opts.openai_model
    if not api_key:
        return web.json_response({"ok": False, "error": "No API key entered."})
    return web.json_response(await openai_engine.test(api_key, model))


# Startup --------------------------------------------------------------------


def _zone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        _LOGGER.warning("Unknown time zone %s", name)
        return None


async def resolve_time_zone(ha: HomeAssistant, attempts: int = 20) -> tuple[tzinfo, str]:
    """(time zone, where it came from). Home Assistant may still be starting when the add-on
    starts, so it is asked several times before falling back."""
    for attempt in range(attempts):
        zone = _zone(await ha.time_zone())
        if zone:
            return zone, "home_assistant"
        if attempt + 1 < attempts:
            await asyncio.sleep(3)
    zone = _zone(container_env("TZ"))
    if zone:
        _LOGGER.warning("Home Assistant did not report its time zone; using the container's TZ (%s)", zone)
        return zone, "container"
    _LOGGER.error("Could not determine the time zone; using UTC until Home Assistant answers")
    return timezone.utc, "fallback"


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().addHandler(LOG_BUFFER)
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per HTTP request is noise
    opts = load_settings()
    os.makedirs(DATA_DIR, exist_ok=True)
    backup.cleanup(0)
    db = Database(os.path.join(DATA_DIR, "batteryai.db"))
    db.mark_interrupted()

    async with aiohttp.ClientSession() as session:
        ha = HomeAssistant(session)
        tz, tz_source = await resolve_time_zone(ha)
        batteryai = BatteryAI(opts, db, ha, tz)
        batteryai.tz_source = tz_source
        batteryai.panel_url = await ha.panel_path()

        app = web.Application(middlewares=[revalidate_static])
        app["batteryai"] = batteryai
        app["background"] = set()
        app.add_routes(routes)
        app.router.add_static("/static/", STATIC_DIR)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        _LOGGER.info("BatteryAI UI listening on port %d", PORT)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        batteryai.relocalize()
        batteryai.start_loops()
        if tz_source != "home_assistant":
            batteryai.spawn(batteryai.retry_time_zone())
        batteryai.import_history_if_empty()
        await stop.wait()
        _LOGGER.info("Shutting down")
        batteryai.stop_loops()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
