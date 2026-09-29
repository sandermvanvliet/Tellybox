"""The admin JSON API for Home Assistant and friends (HA-1..HA-8), `docs/admin-api.md`.

Every action goes through the cast service, like the dashboard's buttons; nothing here bypasses the timer.
All handlers are async so the shared sqlite connection is only used from the event loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tellybox import api_tokens
from tellybox.clock import Clock
from tellybox.config import Config
from tellybox.web.api.guard import TokenGuard
from tellybox.web.api.state import API_VERSION, build_admin_state, unreachable
from tellybox.web.cast_client import CastUnavailable
from tellybox.web.hub import SSE_KEEPALIVE_S as _DEFAULT_KEEPALIVE_S
from tellybox.web.hub import KidHub, sse_stream
from tellybox.web.overrides import apply_override

log = logging.getLogger(__name__)

SSE_KEEPALIVE_S = _DEFAULT_KEEPALIVE_S
CAPABILITIES = ["state", "events", "overrides", "profiles"]


class BadRequest(ValueError):
    pass


def _detail(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def _body(request: Request, allowed: set[str]) -> dict:
    """The JSON object body: absent or empty is `{}`; anything but an object with known keys is refused."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise BadRequest("body must be JSON") from None
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise BadRequest(f"unknown field {unknown[0]!r}")
    return body


def _int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"{name} must be an integer")
    return value


def _profile_ids(value) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise BadRequest("profile_ids must be a list of integers")
    return [_int(v, "profile_ids") for v in value]


def _parse_query_ids(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    try:
        return [int(part) for part in raw.split(",")]
    except ValueError:
        raise BadRequest("profile_ids must be comma-separated integers") from None


def create_router(config: Config, conn: sqlite3.Connection, clock: Clock, cast, admin_hub: KidHub, counts) -> APIRouter:
    router = APIRouter()
    read = Depends(TokenGuard(conn, clock, api_tokens.READ))
    control = Depends(TokenGuard(conn, clock, api_tokens.CONTROL))

    def admin_state(cast_state: dict) -> dict:
        return build_admin_state(conn, config, cast_state, counts.get())

    @router.get("/api/info")
    async def info() -> dict:  # HA-6: no auth, so an integration can identify the instance first
        return {"instance_id": api_tokens.instance_id(conn), "version": config.version, "api": API_VERSION,
                "capabilities": CAPABILITIES}

    @router.get("/api/admin/state", dependencies=[read])
    async def state() -> dict:
        try:
            return admin_state(await cast.state())
        except CastUnavailable:
            return unreachable(admin_hub.state)

    @router.get("/api/admin/events", dependencies=[read])
    async def events() -> StreamingResponse:
        return StreamingResponse(sse_stream(admin_hub, SSE_KEEPALIVE_S), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def override(request: Request, kind: str, value: int | None = None,
                       profile_ids: list[int] | None = None) -> JSONResponse:
        token = request.state.api_token
        try:
            cast_state = await apply_override(cast, kind, value, profile_ids, source=token.name)
        except ValueError as exc:  # ours, or the cast service's 422 (unknown profile...)
            return _detail(422, str(exc))
        except CastUnavailable as exc:
            log.warning("admin API override %s: cast service unreachable: %s", kind, exc)
            return _detail(503, "cast_unavailable")
        return JSONResponse(admin_state(cast_state))

    async def parsed(request: Request, allowed: set[str], build) -> JSONResponse:
        try:
            args = build(await _body(request, allowed))
        except BadRequest as exc:
            return _detail(422, str(exc))
        return await override(request, **args)

    @router.post("/api/admin/overrides/extra", dependencies=[control])
    async def extra(request: Request) -> JSONResponse:
        def build(body: dict) -> dict:
            if "minutes" not in body:
                raise BadRequest("minutes is required")
            return {"kind": "extra_minutes", "value": _int(body["minutes"], "minutes"),
                    "profile_ids": _profile_ids(body.get("profile_ids"))}
        return await parsed(request, {"minutes", "profile_ids"}, build)

    @router.post("/api/admin/overrides/unlimited", dependencies=[control])
    async def unlimited(request: Request) -> JSONResponse:
        return await parsed(request, {"profile_ids"},
                            lambda b: {"kind": "unlimited", "profile_ids": _profile_ids(b.get("profile_ids"))})

    @router.post("/api/admin/overrides/block", dependencies=[control])
    async def block(request: Request) -> JSONResponse:
        return await parsed(request, {"profile_ids"},
                            lambda b: {"kind": "block", "profile_ids": _profile_ids(b.get("profile_ids"))})

    @router.post("/api/admin/overrides/stop", dependencies=[control])
    async def stop(request: Request) -> JSONResponse:
        return await parsed(request, set(), lambda b: {"kind": "stop_now"})

    @router.delete("/api/admin/overrides/today", dependencies=[control])
    async def clear(request: Request) -> JSONResponse:
        return await parsed(request, set(), lambda b: {
            "kind": "clear", "profile_ids": _parse_query_ids(request.query_params.get("profile_ids"))})

    return router
