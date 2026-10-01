"""BatteryAI add-on: records sensors, runs scheduled Claude analyses, serves the ingress UI."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import anthropic
from aiohttp import web

from analyzer import AnalysisError, analyze, build_input
from collector import collect, has_data
from config import DATA_DIR, Options, load_options
from db import Database
from ha import HomeAssistant

PORT = int(os.environ.get("BATTERYAI_PORT", "8099"))
STATIC_DIR = Path(__file__).parent / "static"
MAX_CHART_POINTS = 2500

_LOGGER = logging.getLogger("batteryai")


class BatteryAI:
    def __init__(self, opts: Options, db: Database, ha: HomeAssistant, tz: tzinfo) -> None:
        self.opts = opts
        self.db = db
        self.ha = ha
        self.tz = tz
        self.next_analysis: datetime | None = None
        self.last_snapshot: dict[str, Any] | None = None
        self._analysis_lock = asyncio.Lock()
        self._client = (
            anthropic.AsyncAnthropic(api_key=opts.claude_api_key, max_retries=3, timeout=600.0)
            if opts.claude_api_key
            else None
        )

    async def record(self) -> dict[str, Any] | None:
        snapshot = await collect(self.ha, self.opts, self.tz)
        self.last_snapshot = snapshot
        if not has_data(snapshot):
            _LOGGER.warning("No sensor values available; reading not stored")
            return None
        self.db.add_reading(snapshot, self.tz)
        return snapshot

    async def recorder_loop(self) -> None:
        interval = self.opts.record_interval_minutes * 60
        while True:
            try:
                await self.record()
            except Exception:  # keep recording even if one cycle fails
                _LOGGER.exception("Recording failed")
            await asyncio.sleep(interval - time.time() % interval)

    async def scheduler_loop(self) -> None:
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in self.opts.analysis_times())
        _LOGGER.info("Scheduled analyses at %s (%s)", times, self.tz)
        while True:
            self.next_analysis = self.opts.next_analysis(datetime.now(self.tz), self.tz)
            while (remaining := (self.next_analysis - datetime.now(self.tz)).total_seconds()) > 0:
                await asyncio.sleep(min(remaining, 60))
            try:
                await self.run_analysis("schedule")
            except Exception:
                _LOGGER.exception("Scheduled analysis failed")

    @property
    def analysis_running(self) -> bool:
        return self._analysis_lock.locked()

    async def run_analysis(self, trigger: str) -> int:
        async with self._analysis_lock:
            analysis_id = self.db.start_analysis(trigger, self.opts.claude_model)
            _LOGGER.info("Starting analysis #%d (%s)", analysis_id, trigger)
            data: dict[str, Any] | None = None
            try:
                if self._client is None:
                    raise AnalysisError("Set claude_api_key in the add-on configuration.")
                snapshot = await self.record() or self.last_snapshot
                if snapshot is None or not has_data(snapshot):
                    raise AnalysisError("No sensor data available from Home Assistant.")
                data = build_input(self.db, self.opts, snapshot, self.tz)
                outcome = await analyze(self._client, self.opts, data)
            except AnalysisError as err:
                _LOGGER.error("Analysis #%d failed: %s", analysis_id, err)
                self.db.finish_analysis(analysis_id, status="error", error=str(err), input_data=data)
            except Exception as err:
                _LOGGER.exception("Analysis #%d crashed", analysis_id)
                self.db.finish_analysis(analysis_id, status="error", error=f"Unexpected error: {err}", input_data=data)
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
            return analysis_id


# HTTP API -------------------------------------------------------------------

routes = web.RouteTableDef()


def _app(request: web.Request) -> BatteryAI:
    return request.app["batteryai"]


def _int_param(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(request.query.get(name, default))))
    except ValueError:
        return default


@routes.get("/")
async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    app = _app(request)
    opts = app.opts
    latest = app.db.latest_reading()
    warnings = []
    if not opts.claude_api_key:
        warnings.append("Claude API key is not set.")
    if len(opts.deye_programs) != 6:
        warnings.append(f"{len(opts.deye_programs)} Deye programs configured; Deye inverters have 6.")
    if app.last_snapshot and app.last_snapshot["missing_entities"]:
        warnings.append("Unavailable entities: " + ", ".join(app.last_snapshot["missing_entities"]))
    return web.json_response(
        {
            "time_zone": str(app.tz),
            "model": opts.claude_model,
            "analysis_times": [f"{h:02d}:{m:02d}" for h, m in opts.analysis_times()],
            "next_analysis": app.next_analysis.isoformat(timespec="minutes") if app.next_analysis else None,
            "analysis_running": app.analysis_running,
            "record_interval_minutes": opts.record_interval_minutes,
            "reading_count": app.db.reading_count(),
            "latest": latest,
            "units": app.last_snapshot["units"] if app.last_snapshot else {},
            "active_program_slot": app.last_snapshot["active_program_slot"] if app.last_snapshot else None,
            "sensors": opts.sensor_map(),
            "warnings": warnings,
        }
    )


@routes.get("/api/readings")
async def readings(request: web.Request) -> web.Response:
    hours = _int_param(request, "hours", 48, 1, 24 * 90)
    rows = _app(request).db.readings_since(int(time.time()) - hours * 3600)
    step = max(1, len(rows) // MAX_CHART_POINTS)
    keys = ("ts", "battery_soc", "target_soc", "today_load", "today_consumption", "today_forecast")
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


@routes.post("/api/analyze")
async def analyze_now(request: web.Request) -> web.Response:
    app = _app(request)
    if app.analysis_running:
        return web.json_response({"error": "An analysis is already running."}, status=409)
    request.app["background"].add(task := asyncio.create_task(app.run_analysis("manual")))
    task.add_done_callback(request.app["background"].discard)
    return web.json_response({"started": True}, status=202)


# Startup --------------------------------------------------------------------


async def resolve_time_zone(ha: HomeAssistant) -> tzinfo:
    for name in (await ha.time_zone(), os.environ.get("TZ")):
        if name:
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError:
                _LOGGER.warning("Unknown time zone %s", name)
    _LOGGER.warning("Could not determine the Home Assistant time zone; using UTC")
    return timezone.utc


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    opts = load_options()
    os.makedirs(DATA_DIR, exist_ok=True)
    db = Database(os.path.join(DATA_DIR, "batteryai.db"))
    db.mark_interrupted()

    async with aiohttp.ClientSession() as session:
        ha = HomeAssistant(session)
        tz = await resolve_time_zone(ha)
        batteryai = BatteryAI(opts, db, ha, tz)

        app = web.Application()
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

        tasks = [
            asyncio.create_task(batteryai.recorder_loop()),
            asyncio.create_task(batteryai.scheduler_loop()),
        ]
        await stop.wait()
        _LOGGER.info("Shutting down")
        for task in tasks:
            task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
