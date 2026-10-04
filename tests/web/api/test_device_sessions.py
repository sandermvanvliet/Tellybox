"""Admin overrides reach in-app sessions (WT-12, HA-8): the web app against a real CastController, in-process."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from tellybox import api_tokens, library
from tellybox.cast.api import create_api
from tellybox.cast.controller import CastController
from tellybox.cast.fake import FakeCastDevice
from tellybox.clock import FakeClock
from tellybox.db import open_db
from tellybox.web.app import create_app
from tellybox.web.cast_client import CastClient
from tests.web.conftest import make_config

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
DEV = "device-aaaa1111"


@pytest.fixture
async def env(tmp_path):
    config = make_config(tmp_path)
    (config.media_dir / "dev").mkdir(parents=True)
    (config.media_dir / "dev" / "ep1.mp4").write_bytes(b"x" * 1000)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>")
    conn = open_db(tmp_path / "tellybox.db")
    clock = FakeClock(NOW)
    show = library.create_show(conn, "Dev show", now=NOW)
    episode = library.add_episode(conn, show, "Episode 1", "dev/ep1.mp4", now=NOW, duration_s=600)
    conn.execute("UPDATE profile SET watch_in_app = 1")
    ctrl = CastController(conn, clock=clock, tz=ZoneInfo("Europe/Amsterdam"), media_base_url="http://tv.test:8080",
                          secret=config.secret, device=FakeCastDevice(clock))
    await ctrl.start(run_loops=False)
    cast_app = create_api(ctrl, conn, discover=lambda: None, device_factory=lambda info: FakeCastDevice(clock))
    cast = CastClient("http://cast", transport=httpx.ASGITransport(app=cast_app))
    app = create_app(config, conn=conn, clock=clock, cast=cast, static_dir=static, ytdlp=SimpleNamespace())
    _, token = api_tokens.create_token(conn, "Home Assistant", ["read", "control"], NOW)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://web") as client:
        yield SimpleNamespace(client=client, episode=episode, auth={"Authorization": f"Bearer {token}"}, conn=conn)
    await cast.aclose()
    conn.close()


async def start_in_app(env) -> str:
    r = await env.client.post("/api/kid/play", json={"episode_id": env.episode, "profile_ids": [1],
                                                     "target": "device", "device_id": DEV},
                              headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/124.0 Mobile Safari/537.36"})
    assert r.status_code == 200, r.text
    return r.json()["url"]


async def test_stop_now_ends_an_in_app_session(env):
    url = await start_in_app(env)
    assert (await env.client.get(url)).status_code == 200  # served while the session is open

    state = (await env.client.get("/api/admin/state", headers=env.auth)).json()
    assert [(s["target"], s["label"], s["profile_ids"]) for s in state["sessions"]] == [("device", "Android Chrome", [1])]

    r = await env.client.post("/api/admin/overrides/stop", json={}, headers=env.auth)
    assert r.status_code == 200 and r.json()["sessions"] == []

    assert (await env.client.get(url)).status_code == 403  # the media token is revoked (WT-11)
    beat = await env.client.post("/api/kid/device/heartbeat",
                                 json={"device_id": DEV, "state": "playing", "position_s": 20})
    assert beat.json()["action"] == "stop" and beat.json()["reason"] == "stop_now"
    row = env.conn.execute("SELECT target, device_label, end_reason FROM watch_session").fetchone()
    assert tuple(row) == ("device", "Android Chrome", "parent_stop")


async def test_block_ends_an_in_app_session(env):
    url = await start_in_app(env)
    r = await env.client.post("/api/admin/overrides/block", json={}, headers=env.auth)
    assert r.status_code == 200 and r.json()["sessions"] == []
    assert (await env.client.get(url)).status_code == 403
