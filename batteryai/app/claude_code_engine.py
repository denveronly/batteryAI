"""Claude through a Claude Code bridge: the same input, instructions and result schema as the
Claude API engine, sent to a small HTTP bridge that runs `claude -p` with the owner's own
Claude Code login (for example inside the Claude Code add-on for Home Assistant).

Bridge contract (POST {url}/v1/complete, header "Authorization: Bearer <token>"):
  request  {"system", "prompt", "model", "effort", "json_schema", "timeout"}
  response {"text", "structured_output", "model", "seconds", "input_tokens", "output_tokens"}
Errors come back as non-200 with {"error": "...", "detail": "..."}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from analyzer import SYSTEM_PROMPT, AnalysisError, result_schema
from config import Options

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 900  # the bridge allows up to 15 min; Opus at high effort on 14 days of data takes a few


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _error_message(status: int, body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    detail = body.get("detail") if isinstance(body, dict) else None
    if status == 401:
        return "The Claude Code bridge rejected the token. Check it in the Settings tab."
    if status == 429:
        return "The Claude Code bridge is busy with another request; the next scheduled run will try again."
    if status == 504:
        return f"Claude Code did not answer in time ({error or 'timeout'})."
    if status == 404:
        return "The Claude Code bridge has no /v1/complete endpoint – check the URL."
    return f"Claude Code bridge error {status}: {error or body}" + (f" – {detail}" if detail else "")


async def _post(url: str, token: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url.rstrip("/") + "/v1/complete", headers=_headers(token), json=payload) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError):
                    body = {"error": (await resp.text())[:300]}
                return resp.status, body
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise AnalysisError(f"Could not reach the Claude Code bridge at {url}: {err or type(err).__name__}") from err


async def analyze(opts: Options, data: dict[str, Any]) -> dict[str, Any]:
    """Returns {"result", "model", "input_tokens", "output_tokens"}; raises AnalysisError."""
    if not opts.claude_code_url or not opts.claude_code_token:
        raise AnalysisError("Set the Claude Code bridge URL and token in the Settings tab.")
    user_text = (
        f"Analyse this data and produce the plan. Write all text fields in {opts.response_language}.\n\n"
        + json.dumps(data, ensure_ascii=False, default=str)
    )
    payload = {
        "system": SYSTEM_PROMPT,
        "prompt": user_text,
        "model": opts.claude_model,
        "effort": opts.claude_effort,
        "json_schema": result_schema(opts),
        "timeout": REQUEST_TIMEOUT - 30,
    }
    started = time.monotonic()
    status, body = await _post(opts.claude_code_url, opts.claude_code_token, payload, REQUEST_TIMEOUT)
    if status != 200:
        raise AnalysisError(_error_message(status, body))
    _LOGGER.info(
        "Claude Code analysis finished in %.1fs (model %s, bridge %.1fs)",
        time.monotonic() - started, body.get("model"), body.get("seconds") or 0,
    )
    result = body.get("structured_output")
    if not isinstance(result, dict):
        text = body.get("text") or ""
        try:
            result = json.loads(text)
        except json.JSONDecodeError as err:
            raise AnalysisError(f"Claude Code returned no plan JSON: {text[:200]!r}") from err
    return {
        "result": result,
        "model": body.get("model") or opts.claude_model,
        "input_tokens": body.get("input_tokens"),
        "output_tokens": body.get("output_tokens"),
    }


async def test(url: str, token: str) -> dict[str, Any]:
    """Checks that the bridge answers and accepts the token, without running Claude:
    an empty prompt passes the token check and is then refused with HTTP 400."""
    if not url or not token:
        return {"ok": False, "error": "Enter the bridge URL and token."}
    try:
        status, body = await _post(url, token, {"prompt": ""}, 15)
    except AnalysisError as err:
        return {"ok": False, "error": str(err)}
    if status == 400:
        return {"ok": True}
    return {"ok": False, "error": _error_message(status, body)}
