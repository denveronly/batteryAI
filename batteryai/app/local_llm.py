"""Local slow engine: Qwen2.5-3B-Instruct (GGUF, Q4_K_M) running on the CPU inside the add-on.

llama.cpp is compiled into the add-on image (llama-cpp-python, see the Dockerfile). The
model file is downloaded once into /data/models (excluded from backups). Each prediction
runs in a separate worker process, so the ~2.5 GB of model memory is released afterwards.

The LLM does not read the raw history: the local fast engine's forecast is its input, and
the LLM writes the plan, the reasoning and the recommendations.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

from config import DATA_DIR, Options

_LOGGER = logging.getLogger(__name__)

MODEL_NAME = "Qwen2.5-3B-Instruct (Q4_K_M)"
MODEL_FILE = "qwen2.5-3b-instruct-q4_k_m.gguf"
MODEL_URL = f"https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/{MODEL_FILE}"
MODEL_DIR = Path(DATA_DIR) / "models"
MODEL_PATH = MODEL_DIR / MODEL_FILE
WORKER = Path(__file__).with_name("llm_worker.py")
RUN_TIMEOUT = 45 * 60  # a 3B model on a small CPU is slow

LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "outage_risk": {"type": "string", "enum": ["none", "low", "medium", "high", "unknown"]},
        "deye_programs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "soc_percent": {"type": "integer"},
                    "grid_charge": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["slot", "soc_percent", "grid_charge", "reason"],
            },
        },
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["summary", "confidence", "outage_risk", "deye_programs", "recommendations", "reasoning"],
}

SYSTEM_PROMPT = """You plan the battery of a home with solar panels and a Deye hybrid inverter. Answer only with JSON that matches the schema.

The inverter has six programs. Each covers a time range and keeps the battery at or above its SOC (%). grid_charge=true lets it charge from the grid up to that SOC.

Rules:
- Grid power costs what the tariff of the hour says (see tariff and tariff_by_hour). Charge from the grid in the cheapest tariff's programs only as much as the following pricier hours need beyond what PV covers.
- In programs of pricier tariffs let the battery discharge (low SOC, grid_charge false).
- If PV tomorrow covers the use, keep grid charging low and let the sun charge the battery.
- If an outage is expected, keep the battery high and enable grid charge before it.
- Keep every SOC between min_soc and max_soc. Programs marked unused: return their current SOC and grid_charge false.
- The baseline plan from the statistical planner is a good starting point; change it only with a reason.
Write short reasons."""


def available() -> dict[str, Any]:
    """Whether llama.cpp is built into this image and the model is downloaded."""
    return {
        "runtime": importlib.util.find_spec("llama_cpp") is not None,
        "model": MODEL_PATH.exists(),
        "model_name": MODEL_NAME,
        "model_bytes": MODEL_PATH.stat().st_size if MODEL_PATH.exists() else 0,
    }


class ModelDownloader:
    """Downloads the GGUF file into /data/models with progress, resumable via a .part file."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"running": False, "done": 0, "total": 0, "error": None}
        self._task: asyncio.Task | None = None

    def start(self) -> bool:
        if self.state["running"]:
            return False
        self.state = {"running": True, "done": 0, "total": 0, "error": None, "started": time.time()}
        self._task = asyncio.create_task(self._download())
        return True

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _download(self) -> None:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        part = MODEL_PATH.with_suffix(".part")
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(MODEL_URL, headers=headers) as resp:
                    if resp.status == 200:
                        offset = 0  # the server ignored the range: start over
                    elif resp.status != 206:
                        raise RuntimeError(f"HTTP {resp.status} from {MODEL_URL}")
                    self.state["total"] = offset + int(resp.headers.get("Content-Length", 0))
                    self.state["done"] = offset
                    with open(part, "ab" if offset else "wb") as fh:
                        async for chunk in resp.content.iter_chunked(1 << 20):
                            fh.write(chunk)
                            self.state["done"] += len(chunk)
            if self.state["total"] and self.state["done"] < self.state["total"]:
                raise RuntimeError("download ended early; press Download again to resume")
            os.replace(part, MODEL_PATH)
            _LOGGER.info("Downloaded %s (%d bytes)", MODEL_FILE, self.state["done"])
        except asyncio.CancelledError:
            self.state["error"] = "Cancelled"
            raise
        except Exception as err:
            _LOGGER.error("Model download failed: %s", err)
            self.state["error"] = str(err)
        finally:
            self.state["running"] = False


def delete_model() -> None:
    for path in (MODEL_PATH, MODEL_PATH.with_suffix(".part")):
        if path.exists():
            path.unlink()


def build_prompt(opts: Options, snapshot: dict[str, Any], baseline: dict[str, Any]) -> str:
    programs = []
    for program in snapshot.get("deye_programs") or []:
        base = next((p for p in baseline["deye_programs"] if p["slot"] == program["slot"]), {})
        programs.append({
            "slot": program["slot"],
            "range": program.get("range"),
            "unused": not program.get("range") or program["range"].split("-")[0] == program["range"].split("-")[-1],
            "current_soc": program.get("soc"),
            "has_grid_charge_switch": program.get("grid_charge") is not None,
            "baseline_soc": base.get("soc_percent"),
            "baseline_grid_charge": base.get("grid_charge"),
        })
    hourly = [
        {"h": e["hour"], "load_w": e["load_w"], "pv_w": pv}
        for e, pv in zip(baseline["hourly_forecast_tomorrow"], baseline.get("_pv_hourly") or [0] * 24)
    ]
    data = {
        "now": snapshot["local_time"],
        "battery_soc": snapshot.get("battery_soc"),
        "battery_capacity_kwh": opts.battery_capacity_kwh,
        "min_soc": opts.min_soc_percent,
        "max_soc": opts.max_soc_percent,
        "safety_margin_percent": opts.prediction_margin_percent,
        "tariff": opts.tariff_dict(),
        "tariff_by_hour": [opts.tariff_at(h * 60 + 30)[1] for h in range(24)],
        "outages_state": snapshot.get("outages_state"),
        "outages_details": snapshot.get("outages_attrs"),
        "weather_tomorrow": (snapshot.get("weather") or {}).get("tomorrow"),
        "forecast": {
            "consumption_tomorrow_kwh": baseline["predicted_consumption_tomorrow_kwh"],
            "pv_tomorrow_kwh": baseline["predicted_pv_tomorrow_kwh"],
            "appliances": [
                {**a, "name": next((x.name for x in opts.appliances if x.id == a["appliance"]), a["appliance"])}
                for a in baseline["appliance_forecast"]
            ],
            "hourly_tomorrow": hourly,
        },
        "programs": programs,
        "owner_notes": opts.extra_instructions or None,
    }
    language = f" Write the text fields in {opts.response_language}." if opts.response_language else ""
    return "Plan the six programs for the next 24 hours." + language + "\n\n" + json.dumps(data, ensure_ascii=False, default=str)


async def run(opts: Options, snapshot: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Runs the model in a worker process; returns the LLM's JSON answer."""
    status = available()
    if not status["runtime"]:
        raise RuntimeError(
            "The local LLM runtime (llama.cpp) is not built into this add-on image. "
            "Rebuild the add-on, or check the add-on build log for errors."
        )
    if not status["model"]:
        raise RuntimeError("The local model is not downloaded yet: Settings → Prediction engine → Download.")
    request = {
        "model_path": str(MODEL_PATH),
        "threads": opts.local_llm_threads or os.cpu_count() or 4,
        "system": SYSTEM_PROMPT,
        "user": build_prompt(opts, snapshot, baseline),
        "schema": LLM_SCHEMA,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(WORKER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), RUN_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        raise RuntimeError(f"The local model did not finish within {RUN_TIMEOUT // 60} minutes.") from None
    if process.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-5:]
        raise RuntimeError("The local model failed: " + " | ".join(tail))
    answer = json.loads(stdout.decode())
    _LOGGER.info("Local model finished in %.0fs (%s tokens)", time.monotonic() - started, answer.get("usage"))
    return answer


def merge(opts: Options, baseline: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """The statistical forecast with the LLM's plan and text; SOC values are clamped."""
    result = dict(baseline)
    by_slot = {p.get("slot"): p for p in answer.get("deye_programs") or [] if isinstance(p, dict)}
    programs = []
    for base in baseline["deye_programs"]:
        suggestion = by_slot.get(base["slot"])
        if not suggestion or base["reason"].startswith("Unused"):
            programs.append(base)
            continue
        soc = min(opts.max_soc_percent, max(opts.min_soc_percent, float(suggestion.get("soc_percent", base["soc_percent"]))))
        programs.append({
            **base,
            "soc_percent": round(soc),
            "grid_charge": bool(suggestion.get("grid_charge")) if base["grid_charge"] is not None else None,
            "reason": str(suggestion.get("reason") or base["reason"]),
        })
    result["deye_programs"] = programs
    for key in ("summary", "confidence", "outage_risk", "reasoning"):
        if answer.get(key):
            result[key] = answer[key]
    if answer.get("recommendations"):
        result["recommendations"] = [str(r) for r in answer["recommendations"]][:8]
    result["predicted_min_soc_percent"] = min((p["soc_percent"] for p in programs), default=opts.min_soc_percent)
    return result
