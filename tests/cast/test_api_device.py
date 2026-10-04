"""The device (browser) endpoints of the cast service API (PB-7, WT-10..WT-12)."""

import pytest

from test_api import env  # noqa: F401  (fixture)

DEV = "device-aaaa1111"


async def play(env, **overrides):
    body = {"device_id": DEV, "label": "iPhone Safari", "episode_id": env["episode"], "profile_ids": [1], **overrides}
    return await env["client"].post("/device/play", json=body)


async def test_play_heartbeat_stop(env):
    r = await play(env)
    assert r.status_code == 200
    body = r.json()
    assert body["start_s"] == 0
    assert body["url"].startswith(f"/media/{env['episode']}/") and "?s=" in body["url"]
    assert body["session"]["key"] == f"device:{DEV}" and body["session"]["state"] == "loading"

    beat = {"device_id": DEV, "state": "playing", "position_s": 3.5, "duration_s": 600}
    r = await env["client"].post("/device/heartbeat", json=beat)
    assert r.status_code == 200
    assert r.json() == {"action": "continue", "reason": None, "time_up": False, "grace_deadline": None}
    state = (await env["client"].get("/state")).json()
    assert state["sessions"][0]["state"] == "playing" and state["now_playing"] is None

    r = await env["client"].post("/device/stop", json={"device_id": DEV})
    assert r.status_code == 200 and r.json()["sessions"] == []


async def test_heartbeat_of_an_unknown_session_says_stop(env):
    r = await env["client"].post("/device/heartbeat", json={"device_id": DEV, "state": "paused", "position_s": 0})
    assert r.status_code == 200
    assert (r.json()["action"], r.json()["reason"]) == ("stop", "unknown_session")


async def test_stop_of_an_unknown_device_is_a_no_op(env):
    assert (await env["client"].post("/device/stop", json={"device_id": DEV})).status_code == 200


async def test_play_unknown_episode_is_404(env):
    assert (await play(env, episode_id=999)).status_code == 404


async def test_play_unknown_profile_is_422(env):
    assert (await play(env, profile_ids=[1, 42])).status_code == 422


@pytest.mark.parametrize("override", [
    {"device_id": "short"},
    {"device_id": "has spaces in it!"},
    {"device_id": "x" * 65},
    {"label": "L" * 41},
    {"profile_ids": []},
    {"profile_ids": list(range(1, 22))},
])
async def test_play_validation_is_422(env, override):
    assert (await play(env, **override)).status_code == 422


@pytest.mark.parametrize("body", [
    {"device_id": DEV, "state": "dancing", "position_s": 0},
    {"device_id": DEV, "state": "playing", "position_s": -1},
    {"device_id": DEV, "state": "playing", "position_s": 1, "duration_s": 0},
    {"device_id": "short", "state": "playing", "position_s": 1},
    {"state": "playing", "position_s": 1},
])
async def test_heartbeat_validation_is_422(env, body):
    assert (await env["client"].post("/device/heartbeat", json=body)).status_code == 422


async def test_play_refused_when_blocked_is_409(env):  # KA-9, WT-7
    assert (await env["client"].post("/overrides", json={"kind": "block"})).status_code == 200
    r = await play(env)
    assert r.status_code == 409
    assert r.json()["detail"] == {"error": "time_up", "reason": "blocked"}


async def test_a_device_session_needs_no_chromecast(env):
    env["ctrl"].device = None
    assert (await play(env)).status_code == 200
    state = (await env["client"].get("/state")).json()
    assert state["device"] is None and len(state["sessions"]) == 1


async def test_block_through_the_api_ends_the_session(env):  # WT-12
    await play(env)
    assert (await env["client"].post("/overrides", json={"kind": "block"})).status_code == 200
    r = await env["client"].post("/device/heartbeat", json={"device_id": DEV, "state": "playing", "position_s": 1})
    assert (r.json()["action"], r.json()["reason"]) == ("stop", "blocked")
