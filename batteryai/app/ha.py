"""Minimal Home Assistant REST client (through the Supervisor proxy)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class HomeAssistant:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # HA_URL / HA_TOKEN allow running outside the Supervisor for development.
        base = os.environ.get("HA_URL", "http://supervisor/core").rstrip("/")
        token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN", "")
        self._api = f"{base}/api"
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._timeout = aiohttp.ClientTimeout(total=15)
        self._missing_logged: set[str] = set()

    async def _get(self, path: str) -> Any | None:
        try:
            async with self._session.get(
                f"{self._api}{path}", headers=self._headers, timeout=self._timeout
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Home Assistant request %s failed: %s", path, err)
            return None

    async def state(self, entity_id: str) -> dict[str, Any] | None:
        if not entity_id:
            return None
        result = await self._get(f"/states/{entity_id}")
        if result is None and entity_id not in self._missing_logged:
            self._missing_logged.add(entity_id)
            _LOGGER.warning("Entity %s not found or unavailable", entity_id)
        elif result is not None:
            self._missing_logged.discard(entity_id)
        return result

    async def time_zone(self) -> str | None:
        config = await self._get("/config")
        return config.get("time_zone") if isinstance(config, dict) else None
