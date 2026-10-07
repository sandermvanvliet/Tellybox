"""Internal HTTP API of the cast service (localhost only; the web app proxies it).

Commands go in as POSTs; state comes out as a snapshot (GET /state) or a live
SSE stream (GET /events, KA-7).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from tellybox import store
from tellybox.cast.controller import CastController, NoDevice, PlayRefused
from tellybox.cast.common import ShowNotAllowed
from tellybox.cast.device_sessions import LABEL_MAX
from tellybox.cast.device import CastDevice, DeviceInfo
from tellybox.cast.pychromecast_device import CastCommandError

SSE_KEEPALIVE_S = 15.0

Discover = Callable[[], Awaitable[list[DeviceInfo]]]
DeviceFactory = Callable[[DeviceInfo], CastDevice]


class PlayRequest(BaseModel):
    episode_id: int
    profile_ids: list[int] = Field(min_length=1, max_length=20)  # PR-2: who is watching


DEVICE_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class DevicePlayRequest(BaseModel):
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)  # PB-8: random id the browser keeps
    label: str = Field(max_length=LABEL_MAX)  # a short browser label such as "iPhone Safari"
    episode_id: int
    profile_ids: list[int] = Field(min_length=1, max_length=20)


class DeviceHeartbeatRequest(BaseModel):  # WT-10
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    state: Literal["playing", "paused", "buffering", "ended", "error"]
    position_s: float = Field(ge=0, allow_inf_nan=False)
    duration_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class DeviceStopRequest(BaseModel):
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)


class OverrideRequest(BaseModel):
    kind: Literal["extra_minutes", "unlimited", "block", "stop_now", "clear"]
    value: int | None = None
    profile_ids: list[int] | None = Field(default=None, min_length=1, max_length=20)  # HA-3; None: every profile
    profile_id: int | None = None  # v2 spelling, still accepted
    source: str | None = Field(default=None, max_length=64)  # HA-7: the API token's name

    @model_validator(mode="after")
    def _one_target_spelling(self) -> "OverrideRequest":
        if self.profile_id is not None and self.profile_ids is not None:
            raise ValueError("send profile_id or profile_ids, not both")
        return self


class SelectDeviceRequest(BaseModel):
    uuid: str


def create_api(
    controller: CastController,
    conn: sqlite3.Connection,
    discover: Discover,
    device_factory: DeviceFactory,
) -> FastAPI:
    app = FastAPI(title="Tellybox cast controller")

    async def run(command: Awaitable[None]) -> dict:
        try:
            await command
        except NoDevice:
            raise HTTPException(503, "no Chromecast selected") from None
        except CastCommandError as exc:
            raise HTTPException(502, f"Chromecast command failed: {exc}") from None
        return controller.state()

    @app.get("/state")
    async def state() -> dict:
        return controller.state()

    @app.post("/play")
    async def play(req: PlayRequest) -> dict:
        try:
            return await run(controller.play(req.episode_id, req.profile_ids))
        except KeyError:
            raise HTTPException(404, "no such episode") from None
        except ValueError as exc:  # UnknownProfile
            raise HTTPException(422, f"unknown profile: {exc}") from None
        except ShowNotAllowed:
            raise HTTPException(403, "show not allowed") from None  # PR-7
        except PlayRefused as exc:
            raise HTTPException(409, {"error": "time_up", "reason": exc.decision.reason}) from None

    @app.post("/device/play")
    async def device_play(req: DevicePlayRequest) -> dict:
        try:
            return await controller.device_play(req.device_id, req.label, req.episode_id, req.profile_ids)
        except KeyError:
            raise HTTPException(404, "no such episode") from None
        except ValueError as exc:  # UnknownProfile
            raise HTTPException(422, f"unknown profile: {exc}") from None
        except ShowNotAllowed:
            raise HTTPException(403, "show not allowed") from None  # PR-7
        except PlayRefused as exc:
            raise HTTPException(409, {"error": "time_up", "reason": exc.decision.reason}) from None

    @app.post("/device/heartbeat")
    async def device_heartbeat(req: DeviceHeartbeatRequest) -> dict:
        return await controller.device_heartbeat(req.device_id, req.state, req.position_s, req.duration_s)

    @app.post("/device/stop")
    async def device_stop(req: DeviceStopRequest) -> dict:
        await controller.device_stop(req.device_id)
        return controller.state()

    @app.post("/pause")
    async def pause() -> dict:
        return await run(controller.pause())

    @app.post("/resume")
    async def resume() -> dict:
        return await run(controller.resume())

    @app.post("/stop")
    async def stop() -> dict:
        return await run(controller.stop())

    @app.post("/overrides")
    async def override(req: OverrideRequest) -> dict:
        try:
            ids = [req.profile_id] if req.profile_id is not None else req.profile_ids
            return await run(controller.override(req.kind, req.value, ids, req.source))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/events")
    async def events() -> StreamingResponse:
        queue = controller.subscribe()

        async def stream():
            try:
                while True:
                    try:
                        snapshot = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_S)
                        yield f"data: {json.dumps(snapshot)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                controller.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/typed-events")
    async def typed_events() -> StreamingResponse:
        """HA-13: discrete events (playback, timer, overrides) as ``data: <event>`` frames. No replay; the state
        stream (``/events``) is separate and unchanged."""
        queue = controller.subscribe_events()

        async def stream():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_S)
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                controller.unsubscribe_events(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/devices")
    async def devices() -> dict:
        found = await discover()
        store.remember_devices(conn, found, controller.clock.now())
        selected = store.selected_device(conn)
        return {
            "selected": selected.uuid if selected else None,
            "devices": [d.__dict__ for d in found],
        }

    @app.post("/devices/select")
    async def select_device(req: SelectDeviceRequest) -> dict:
        row = conn.execute("SELECT 1 FROM cast_device WHERE uuid = ?", (req.uuid,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown device; list /devices first")
        store.select_device(conn, req.uuid)
        info = store.selected_device(conn)
        assert info is not None
        await controller.set_device(device_factory(info))
        return controller.state()

    return app
