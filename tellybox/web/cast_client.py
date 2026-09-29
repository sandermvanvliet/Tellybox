"""HTTP client for the cast service's internal API (tellybox.cast.api)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from tellybox.config import Config

# Discovery on the cast service listens for mDNS for 8 s (pychromecast_device.discover).
DEVICES_TIMEOUT_S = 15.0
# The cast service sends a keepalive comment every 15 s; anything much longer means it is gone.
EVENTS_READ_TIMEOUT_S = 40.0


class CastUnavailable(Exception):
    """The cast service is unreachable, has no Chromecast, or a command failed."""


class CastNotFound(Exception):
    """The cast service doesn't know the episode."""


class TimeUp(Exception):
    """The pick was refused because time is up (KA-9); carries the current cast state."""

    def __init__(self, state: dict) -> None:
        super().__init__("time_up")
        self.state = state


class CastClient:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 5.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)

    @classmethod
    def from_config(cls, config: Config) -> CastClient:
        return cls(f"http://{config.cast_api_host}:{config.cast_api_port}")

    @property
    def base_url(self) -> httpx.URL:
        return self._http.base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CastUnavailable(f"{method} {path}: {exc!r}") from exc

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        if r.status_code != 200:
            raise CastUnavailable(f"{r.request.method} {r.request.url.path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    async def state(self) -> dict:
        return self._json(await self._request("GET", "/state"))

    async def play(self, episode_id: int, profile_ids: list[int]) -> dict:
        """PR-2: start an episode for a group of profiles. A 409 means someone in it is out of time (PR-4)."""
        r = await self._request("POST", "/play", json={"episode_id": episode_id, "profile_ids": list(profile_ids)})
        if r.status_code == 409:
            raise TimeUp(await self.state())
        if r.status_code == 404:
            raise CastNotFound(episode_id)
        return self._json(r)

    async def pause(self) -> dict:
        return self._json(await self._request("POST", "/pause"))

    async def resume(self) -> dict:
        return self._json(await self._request("POST", "/resume"))

    async def stop(self) -> dict:
        return self._json(await self._request("POST", "/stop"))

    async def override(
        self, kind: str, value: int | None = None, profile_ids: list[int] | None = None, source: str | None = None
    ) -> dict:
        """WT-7: an admin override for these profiles (None = every profile). `source` names the API token
        that applied it (HA-7; None = the admin pages). A 422 (bad kind, value or profile) raises ValueError."""
        body = {"kind": kind, "value": value, "profile_ids": None if profile_ids is None else list(profile_ids),
                "source": source}
        r = await self._request("POST", "/overrides", json=body)
        if r.status_code == 422:
            raise ValueError(self._detail(r))
        return self._json(r)

    async def devices(self) -> dict:
        """PB-1: trigger a Chromecast search; `{"selected": uuid | None, "devices": [...]}`."""
        return self._json(await self._request("GET", "/devices", timeout=DEVICES_TIMEOUT_S))

    async def select_device(self, uuid: str) -> dict:
        """PB-1. A 404 (unknown device; search first) raises CastNotFound."""
        r = await self._request("POST", "/devices/select", json={"uuid": uuid})
        if r.status_code == 404:
            raise CastNotFound(uuid)
        return self._json(r)

    @staticmethod
    def _detail(r: httpx.Response) -> str:
        try:
            body = r.json()
        except ValueError:
            return r.text
        return body.get("detail", r.text) if isinstance(body, dict) else r.text

    async def events(self) -> AsyncIterator[dict]:
        """Cast state snapshots from the SSE stream (KA-7); ends when the stream does."""
        timeout = httpx.Timeout(5.0, read=EVENTS_READ_TIMEOUT_S)
        try:
            async with self._http.stream("GET", "/events", timeout=timeout) as r:
                if r.status_code != 200:
                    raise CastUnavailable(f"GET /events: HTTP {r.status_code}")
                data: list[str] = []
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
                    elif not line and data:
                        yield json.loads("\n".join(data))
                        data = []
        except httpx.HTTPError as exc:
            raise CastUnavailable(f"GET /events: {exc!r}") from exc
