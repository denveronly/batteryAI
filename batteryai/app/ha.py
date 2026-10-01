"""Minimal Home Assistant REST client (through the Supervisor proxy)."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# s6-overlay (used by the Home Assistant base images) does not pass the container
# environment to CMD; the Supervisor token is only readable from these files then.
S6_ENV_DIRS = ("/run/s6/container_environment", "/var/run/s6/container_environment")


def find_token() -> tuple[str, str]:
    """Returns (token, where it came from)."""
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_TOKEN"):
        if os.environ.get(name):
            return os.environ[name].strip(), f"environment variable {name}"
    for directory in S6_ENV_DIRS:
        for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
            path = Path(directory, name)
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if token:
                return token, str(path)
    return "", "not found"


class HAError(Exception):
    """A failed Home Assistant request, with a message meant for the user."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # not_found | auth | http | connection


class HomeAssistant:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # HA_URL / HA_TOKEN allow running outside the Supervisor for development.
        base = os.environ.get("HA_URL", "http://supervisor/core").rstrip("/")
        self.token, self.token_source = find_token()
        self.base_url = base
        self._api = f"{base}/api"
        self._headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        self._timeout = aiohttp.ClientTimeout(total=15)
        self.last_error: str | None = None
        self._missing_logged: set[str] = set()
        self._logged_error: str | None = None
        if self.token:
            _LOGGER.info("Home Assistant API %s, token from %s", self._api, self.token_source)
        else:
            _LOGGER.error(
                "No Supervisor token found. Check that the add-on has homeassistant_api: true "
                "and restart it."
            )

    async def request(self, path: str) -> Any:
        """GET an API path; raises HAError with a readable reason on failure."""
        if not self.token:
            self.last_error = "No Home Assistant access token (SUPERVISOR_TOKEN missing). Restart the add-on."
            raise HAError("auth", self.last_error)
        try:
            async with self._session.get(
                f"{self._api}{path}", headers=self._headers, timeout=self._timeout
            ) as resp:
                if resp.status == 404:
                    raise HAError("not_found", "Not found in Home Assistant")
                if resp.status in (401, 403):
                    self.last_error = f"Home Assistant refused the token (HTTP {resp.status})."
                    raise HAError("auth", self.last_error)
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    self.last_error = f"Home Assistant returned HTTP {resp.status}: {body}"
                    raise HAError("http", self.last_error)
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self.last_error = f"Cannot reach Home Assistant at {self._api}: {err or type(err).__name__}"
            raise HAError("connection", self.last_error) from err
        self.last_error = None
        return data

    async def fetch_state(self, entity_id: str) -> dict[str, Any]:
        entity_id = entity_id.strip()
        if not entity_id:
            raise HAError("not_found", "No entity configured")
        try:
            return await self.request(f"/states/{entity_id}")
        except HAError as err:
            if err.kind == "not_found":
                raise HAError("not_found", f"Entity {entity_id} does not exist in Home Assistant") from err
            raise

    async def state(self, entity_id: str) -> dict[str, Any] | None:
        """Like fetch_state, but logs (once per entity) and returns None on failure."""
        if not entity_id:
            return None
        try:
            result = await self.fetch_state(entity_id)
        except HAError as err:
            if err.kind != "not_found":
                # Connection/token problems affect every entity: log them once, not per entity.
                if str(err) != self._logged_error:
                    self._logged_error = str(err)
                    _LOGGER.error("%s", err)
            elif entity_id not in self._missing_logged:
                self._missing_logged.add(entity_id)
                _LOGGER.warning("%s", err)
            return None
        self._logged_error = None
        self._missing_logged.discard(entity_id)
        return result

    async def states(self) -> list[dict[str, Any]]:
        return await self.request("/states")

    async def config(self) -> dict[str, Any]:
        return await self.request("/config")

    async def time_zone(self) -> str | None:
        try:
            return (await self.config()).get("time_zone")
        except HAError as err:
            _LOGGER.warning("Could not read Home Assistant config: %s", err)
            return None
