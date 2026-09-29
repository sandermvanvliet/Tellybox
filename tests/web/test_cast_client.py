"""CastClient against a small fake cast service (ASGI, in-process)."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from tellybox.web.cast_client import CastClient, CastNotFound, CastUnavailable, TimeUp

STATE = {"connection": "CONNECTED", "now_playing": None, "timer": {"remaining_s": 60}, "time_up": False}
TIME_UP = {**STATE, "time_up": True}


def fake_cast_app(behaviour: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/state")
    async def state():
        return TIME_UP if behaviour.get("play") == 409 else STATE

    @app.post("/play")
    async def play(body: dict):
        behaviour["played"] = body
        code = behaviour.get("play", 200)
        if code == 409:
            raise HTTPException(409, {"error": "time_up", "reason": "allowance"})
        if code != 200:
            raise HTTPException(code, "nope")
        return {**STATE, "now_playing": {"episode_id": body["episode_id"]}}

    @app.post("/pause")
    async def pause():
        if behaviour.get("pause", 200) != 200:
            raise HTTPException(behaviour["pause"], "no Chromecast selected")
        return STATE

    @app.post("/resume")
    async def resume():
        return STATE

    @app.post("/stop")
    async def stop():
        behaviour["stopped"] = True
        return STATE

    @app.post("/overrides")
    async def overrides(body: dict):
        behaviour["override_body"] = body
        if behaviour.get("override", 200) == 422:
            raise HTTPException(422, "extra_minutes needs a positive value")
        return STATE

    @app.get("/devices")
    async def devices():
        if behaviour.get("devices", 200) != 200:
            raise HTTPException(behaviour["devices"], "nope")
        return {"selected": "abc", "devices": [{"uuid": "abc", "name": "Living Room TV"}]}

    @app.post("/devices/select")
    async def select_device(body: dict):
        behaviour["selected"] = body
        if behaviour.get("select", 200) == 404:
            raise HTTPException(404, "unknown device; list /devices first")
        return STATE

    @app.get("/events")
    async def events():
        async def stream():
            yield f"data: {json.dumps({'n': 1})}\n\n"
            yield ": keepalive\n\n"
            yield f"data: {json.dumps({'n': 2})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


@pytest.fixture
def behaviour():
    return {}


@pytest.fixture
async def client(behaviour):
    c = CastClient("http://cast", transport=httpx.ASGITransport(app=fake_cast_app(behaviour)))
    yield c
    await c.aclose()


async def test_state(client):
    assert await client.state() == STATE


async def test_play(client, behaviour):
    s = await client.play(4, [1, 3])
    assert s["now_playing"] == {"episode_id": 4}
    assert behaviour["played"] == {"episode_id": 4, "profile_ids": [1, 3]}


async def test_play_time_up_raises_with_current_state(client, behaviour):
    behaviour["play"] = 409
    with pytest.raises(TimeUp) as exc:
        await client.play(4, [1])
    assert exc.value.state == TIME_UP


@pytest.mark.parametrize("code", [503, 502, 500])
async def test_play_server_errors_are_unavailable(client, behaviour, code):
    behaviour["play"] = code
    with pytest.raises(CastUnavailable):
        await client.play(4, [1])


async def test_play_unknown_episode(client, behaviour):
    behaviour["play"] = 404
    with pytest.raises(CastNotFound):
        await client.play(4, [1])


async def test_pause_resume(client, behaviour):
    assert await client.pause() == STATE
    assert await client.resume() == STATE
    behaviour["pause"] = 503
    with pytest.raises(CastUnavailable):
        await client.pause()


async def test_stop(client, behaviour):
    assert await client.stop() == STATE
    assert behaviour["stopped"] is True


async def test_override(client, behaviour):
    assert await client.override("extra_minutes", 15, profile_ids=[1, 2], source="Home Assistant") == STATE
    assert behaviour["override_body"] == {"kind": "extra_minutes", "value": 15, "profile_ids": [1, 2],
                                          "source": "Home Assistant"}


async def test_override_default_value_and_profile(client, behaviour):
    await client.override("stop_now")
    assert behaviour["override_body"] == {"kind": "stop_now", "value": None, "profile_ids": None, "source": None}


async def test_override_invalid_raises_value_error(client, behaviour):
    behaviour["override"] = 422
    with pytest.raises(ValueError, match="extra_minutes needs a positive value"):
        await client.override("extra_minutes", -1)


async def test_devices(client):
    result = await client.devices()
    assert result == {"selected": "abc", "devices": [{"uuid": "abc", "name": "Living Room TV"}]}


async def test_devices_unavailable(client, behaviour):
    behaviour["devices"] = 503
    with pytest.raises(CastUnavailable):
        await client.devices()


async def test_select_device(client, behaviour):
    assert await client.select_device("abc") == STATE
    assert behaviour["selected"] == {"uuid": "abc"}


async def test_select_device_not_found(client, behaviour):
    behaviour["select"] = 404
    with pytest.raises(CastNotFound):
        await client.select_device("nope")


async def test_events_parses_sse_and_skips_comments(client):
    got = [e async for e in client.events()]
    assert got == [{"n": 1}, {"n": 2}]


async def test_connection_refused_is_unavailable():
    c = CastClient("http://127.0.0.1:9", timeout=2.0)  # nothing listens on the discard port
    try:
        with pytest.raises(CastUnavailable):
            await asyncio.wait_for(c.state(), 5)
        with pytest.raises(CastUnavailable):
            await asyncio.wait_for(anext(aiter(c.events())), 5)
    finally:
        await c.aclose()


def test_from_config(config):
    c = CastClient.from_config(config)
    assert str(c.base_url) == "http://127.0.0.1:8081"
