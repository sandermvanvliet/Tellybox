"""Watching in the app through the kid API (PB-7, KA-13, WT-10..WT-12): play with a target, heartbeat and stop
proxies, `sessions` in the KidState, and the profile switch (AD-7)."""

import pytest
from fastapi.testclient import TestClient

from tellybox.web.app import create_app
from tests.web.conftest import device_session
from tests.web.test_browser_label import IPHONE_SAFARI

DEV = "device-aaaa1111"
UA = {"User-Agent": IPHONE_SAFARI}


@pytest.fixture
def env(config, lib, fake_cast, tmp_path):
    conn, ids = lib
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>")
    conn.execute("INSERT INTO profile (id, name, sort_order, created_at) VALUES (2, 'Noor', 5, 'x')")
    app = create_app(config, conn=conn, cast=fake_cast, static_dir=static)
    return TestClient(app), conn, ids, fake_cast


def allow(conn, *profile_ids):
    conn.execute(f"UPDATE profile SET watch_in_app = 1 WHERE id IN ({','.join('?' * len(profile_ids))})", profile_ids)


def device_play(client, ids, **overrides):
    body = {"episode_id": ids.b1, "profile_ids": [1], "target": "device", "device_id": DEV, **overrides}
    return client.post("/api/kid/play", json=body, headers=UA)


def device_plays(fake):
    return [c for c in fake.calls if c[0] == "device_play"]


# --------------------------------------------------------------------------- profiles


def test_profiles_carry_watch_in_app(env):  # KA-13, AD-7
    client, conn, *_ = env
    allow(conn, 2)
    flags = {p["profile_id"]: p["watch_in_app"] for p in client.get("/api/kid/profiles").json()}
    assert flags == {1: False, 2: True}


# --------------------------------------------------------------------------- POST /api/kid/play


def test_play_on_the_device(env):
    client, conn, ids, fake = env
    allow(conn, 1)
    r = device_play(client, ids)
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == f"/media/{ids.b1}/1/sig.mp4?s=7" and body["start_s"] == 12.5
    assert body["session"]["key"] == f"device:{DEV}"
    assert body["state"]["sessions"][0]["label"] == "iPhone Safari"
    assert device_plays(fake) == [("device_play", DEV, "iPhone Safari", ids.b1, [1])]
    assert not any(c[0] == "play" for c in fake.calls)  # nothing goes to the TV


def test_the_label_comes_from_the_user_agent_not_the_client(env):
    client, conn, ids, fake = env
    allow(conn, 1)
    r = device_play(client, ids, label="Hacked")  # an unknown field is ignored
    assert r.status_code == 200 and device_plays(fake)[-1][2] == "iPhone Safari"
    r = client.post("/api/kid/play", json={"episode_id": ids.b1, "profile_ids": [1], "target": "device",
                                           "device_id": DEV})  # the test client's own User-Agent
    assert r.status_code == 200 and device_plays(fake)[-1][2] == "Browser"


def test_play_on_the_device_needs_every_profile_allowed(env):
    client, conn, ids, fake = env
    allow(conn, 1)
    r = device_play(client, ids, profile_ids=[1, 2])
    assert r.status_code == 403 and r.json() == {"error": "not_allowed"}
    assert device_plays(fake) == []
    allow(conn, 2)
    assert device_play(client, ids, profile_ids=[1, 2]).status_code == 200


def test_play_on_the_device_is_forbidden_by_default(env):
    client, _, ids, _ = env
    assert device_play(client, ids).status_code == 403


def test_play_on_the_device_hidden_episode_is_404(env):
    client, conn, ids, _ = env
    allow(conn, 1)
    assert device_play(client, ids, episode_id=ids.a3).status_code == 404  # hidden
    assert device_play(client, ids, episode_id=ids.h1).status_code == 404  # hidden show


def test_play_on_the_device_needs_a_valid_device_id(env):
    client, conn, ids, _ = env
    allow(conn, 1)
    assert device_play(client, ids, device_id=None).status_code == 422
    assert device_play(client, ids, device_id="short").status_code == 422
    assert device_play(client, ids, device_id="has spaces in it").status_code == 422


def test_play_on_the_device_time_up_is_409(env):
    client, conn, ids, fake = env
    allow(conn, 1)
    fake.mode = "time_up"
    r = device_play(client, ids)
    assert r.status_code == 409 and r.json()["time_up"] is True


def test_play_on_the_device_cast_errors(env):
    client, conn, ids, fake = env
    allow(conn, 1)
    fake.mode = "not_found"
    assert device_play(client, ids).status_code == 404
    fake.mode = "invalid"
    assert device_play(client, ids).status_code == 422
    fake.mode = "down"
    assert device_play(client, ids).status_code == 503


def test_target_tv_is_today_s_behaviour(env):
    client, _, ids, fake = env
    r = client.post("/api/kid/play", json={"episode_id": ids.b1, "profile_ids": [1], "target": "tv"})
    assert r.status_code == 200 and "url" not in r.json()
    assert ("play", ids.b1, [1]) in fake.calls
    assert client.post("/api/kid/play", json={"episode_id": ids.b1, "target": "moon"}).status_code == 422


# --------------------------------------------------------------------------- heartbeat and stop


def test_heartbeat_is_proxied_unchanged(env):
    client, _, _, fake = env
    fake.heartbeat_result = {"action": "next", "reason": None, "time_up": True,
                             "grace_deadline": "2026-09-28T14:10:00+00:00",
                             "next": {"session": device_session(), "url": "/media/2/1/s.mp4?s=8", "start_s": 0}}
    r = client.post("/api/kid/device/heartbeat",
                    json={"device_id": DEV, "state": "ended", "position_s": 590.5, "duration_s": 600})
    assert r.status_code == 200 and r.json() == fake.heartbeat_result
    assert fake.calls[-1] == ("device_heartbeat", DEV, "ended", 590.5, 600)


@pytest.mark.parametrize("body", [
    {"device_id": "short", "state": "playing", "position_s": 1},
    {"device_id": DEV, "state": "dancing", "position_s": 1},
    {"device_id": DEV, "state": "playing", "position_s": -1},
])
def test_heartbeat_validates(env, body):
    client, *_ = env
    assert client.post("/api/kid/device/heartbeat", json=body).status_code == 422


def test_heartbeat_when_cast_is_down_is_503(env):
    client, _, _, fake = env
    fake.mode = "down"
    r = client.post("/api/kid/device/heartbeat", json={"device_id": DEV, "state": "playing", "position_s": 1})
    assert r.status_code == 503 and r.json() == {"error": "unavailable"}


def test_stop_returns_the_kid_state(env):
    client, _, _, fake = env
    r = client.post("/api/kid/device/stop", json={"device_id": DEV})
    assert r.status_code == 200 and r.json()["sessions"] == []
    assert fake.calls[-1] == ("device_stop", DEV)
    fake.mode = "down"
    assert client.post("/api/kid/device/stop", json={"device_id": DEV}).status_code == 503


# --------------------------------------------------------------------------- KidState.sessions


def test_kid_state_sessions(env, mkstate, mkplaying):
    client, _, ids, fake = env
    tv = {"key": "tv", "target": "tv", "label": "Living Room TV", "device_id": None, "episode_id": ids.b1,
          "show_id": ids.bravo, "title": "Title b1", "state": "paused", "position_s": 1, "duration_s": 600,
          "profile_ids": [1]}
    phone = device_session(episode_id=ids.a1, show_id=ids.alpha, profile_ids=[2, 1], title="Secret title")
    fake.current = mkstate(now_playing=mkplaying(ids.b1, ids.bravo, "paused"), sessions=[tv, phone])
    response = client.get("/api/kid/state")
    state = response.json()
    assert state["sessions"] == [
        {"key": "tv", "target": "tv", "label": "Living Room TV", "device_id": None, "episode_id": ids.b1,
         "show_id": ids.bravo, "profile_ids": [1], "state": "paused"},
        {"key": f"device:{DEV}", "target": "device", "label": "iPhone Safari", "device_id": DEV,
         "episode_id": ids.a1, "show_id": ids.alpha, "profile_ids": [1, 2], "state": "playing"},
    ]
    assert state["now_playing"]["episode_id"] == ids.b1  # the TV fields stay as they were
    assert "Secret title" not in response.text


def test_kid_state_without_sessions(env):
    client, *_ = env
    assert client.get("/api/kid/state").json()["sessions"] == []
