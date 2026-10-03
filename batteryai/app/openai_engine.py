"""ChatGPT (OpenAI API) prediction engine: the same input, instructions and result schema as
Claude, sent to the Chat Completions API with structured outputs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import aiohttp

from analyzer import SYSTEM_PROMPT, AnalysisError, result_schema
from config import Options

_LOGGER = logging.getLogger(__name__)

API_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MAX_COMPLETION_TOKENS = 32000
REQUEST_TIMEOUT = 900  # reasoning models can think for several minutes
RETRY_STATUSES = {500, 502, 503, 504}

FALLBACK_MODELS = ["gpt-5", "gpt-5-mini", "gpt-4.1"]

# Reasoning models (o-series, GPT-5) take reasoning_effort; the chat-only variants do not.
REASONING_MODELS = re.compile(r"^(o\d|gpt-5)(?!.*-chat)")
# Models in /v1/models that cannot write the plan (audio, images, embeddings, …).
NOT_CHAT = re.compile(r"audio|realtime|tts|transcribe|image|search|embedding|moderation|instruct|codex|dall-e|whisper|babbage|davinci")


def supports_reasoning(model: str) -> bool:
    return bool(REASONING_MODELS.search(model))


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _error_message(status: int, body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    message = (error or {}).get("message") if isinstance(error, dict) else None
    code = (error or {}).get("code") if isinstance(error, dict) else None
    if status == 401:
        return "OpenAI rejected the API key. Check it in the Settings tab."
    if status == 429 and code == "insufficient_quota":
        return "The OpenAI account has no credit left (insufficient_quota). Add credit at platform.openai.com."
    if status == 429:
        return "OpenAI rate limit reached; the next scheduled run will try again."
    if status == 404:
        return f"OpenAI model not found or not available to this key: {message or 'HTTP 404'}"
    return f"OpenAI API error {status}: {message or body}"


async def _request(method: str, path: str, api_key: str, payload: dict[str, Any] | None = None,
                   timeout: float = 20, retries: int = 0) -> tuple[int, Any]:
    """(HTTP status, JSON body). Retries server errors and dropped connections."""
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.request(method, API_URL + path, headers=_headers(api_key), json=payload) as resp:
                    try:
                        body = await resp.json(content_type=None)
                    except (json.JSONDecodeError, aiohttp.ContentTypeError):
                        body = {"error": {"message": (await resp.text())[:300]}}
                    if resp.status in RETRY_STATUSES and attempt < retries:
                        _LOGGER.warning("OpenAI returned HTTP %s; retrying", resp.status)
                    else:
                        return resp.status, body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            if attempt >= retries:
                raise AnalysisError(f"Could not reach the OpenAI API: {err or type(err).__name__}") from err
            _LOGGER.warning("OpenAI request failed (%s); retrying", err or type(err).__name__)
        await asyncio.sleep(5 * (attempt + 1))
    raise AnalysisError("OpenAI API request failed")  # not reached


async def analyze(opts: Options, data: dict[str, Any]) -> dict[str, Any]:
    """Returns {"result", "model", "input_tokens", "output_tokens"}; raises AnalysisError."""
    if not opts.openai_api_key:
        raise AnalysisError("Set the OpenAI API key in the Settings tab.")
    user_text = (
        f"Analyse this data and produce the plan. Write all text fields in {opts.response_language}.\n\n"
        + json.dumps(data, ensure_ascii=False, default=str)
    )
    payload: dict[str, Any] = {
        "model": opts.openai_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "battery_plan", "strict": True, "schema": result_schema(opts)},
        },
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
    }
    if supports_reasoning(opts.openai_model):
        payload["reasoning_effort"] = opts.openai_effort

    started = time.monotonic()
    status, body = await _request("POST", "/chat/completions", opts.openai_api_key, payload, REQUEST_TIMEOUT, retries=2)
    if status != 200:
        raise AnalysisError(_error_message(status, body))

    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    _LOGGER.info(
        "OpenAI analysis finished in %.1fs (%s, model %s, finish_reason %s)",
        time.monotonic() - started, body.get("id"), body.get("model"), finish,
    )
    if message.get("refusal"):
        raise AnalysisError(f"ChatGPT declined to answer: {message['refusal']}")
    if finish == "length":
        raise AnalysisError("ChatGPT's answer was cut off (max_completion_tokens reached).")
    text = message.get("content")
    if not text:
        raise AnalysisError(f"ChatGPT returned no text (finish_reason {finish}).")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as err:
        raise AnalysisError(f"ChatGPT returned invalid JSON: {err}") from err

    usage = body.get("usage") or {}
    return {
        "result": result,
        "model": body.get("model") or opts.openai_model,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


async def list_models(api_key: str) -> list[dict[str, str]]:
    """Chat models available to the key, newest first; raises AnalysisError."""
    status, body = await _request("GET", "/models", api_key)
    if status != 200:
        raise AnalysisError(_error_message(status, body))
    models = [m for m in body.get("data") or [] if isinstance(m, dict) and m.get("id")]
    chat = [
        m for m in models
        if re.match(r"(gpt-|o\d|chatgpt-)", m["id"]) and not NOT_CHAT.search(m["id"])
    ]
    chat.sort(key=lambda m: m.get("created") or 0, reverse=True)
    return [{"id": m["id"], "display_name": m["id"]} for m in chat]


async def test(api_key: str, model: str) -> dict[str, Any]:
    """Checks the key and model with the Models API (no tokens are used)."""
    try:
        status, body = await _request("GET", f"/models/{model}", api_key)
    except AnalysisError as err:
        return {"ok": False, "error": str(err)}
    if status == 200:
        return {"ok": True, "model": body.get("id") or model, "display_name": body.get("id") or model}
    if status == 404:
        return {"ok": False, "error": f"The key works, but model '{model}' was not found."}
    return {"ok": False, "error": _error_message(status, body)}
