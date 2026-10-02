"""Admin API fixtures (HA-1..HA-8): an app with a fake cast, one read-only and one control token."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tellybox import api_tokens
from tellybox.clock import FakeClock
from tellybox.web.app import create_app
from tests.web.conftest import NOW


@pytest.fixture
def clock():
    return FakeClock(NOW)


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "kid-static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>Tellybox</title>")
    return d


@pytest.fixture
def api(config, lib, fake_cast, clock, static_dir):
    conn, ids = lib
    conn.execute("UPDATE profile SET name = 'Mila', avatar = 'fox', sort_order = 0 WHERE id = 1")
    conn.execute(
        "INSERT INTO profile (id, name, daily_allowance_min, allowance_mode, counting_mode, max_session_min, max_session_mode, sort_order, created_at)"
        " VALUES (2, 'Noor', 30, 'custom', 'wall_clock', 45, 'custom', 1, ?)", (NOW.isoformat(),)
    )
    app = create_app(config, conn=conn, cast=fake_cast, clock=clock, static_dir=static_dir, ytdlp=SimpleNamespace())
    _, read = api_tokens.create_token(conn, "Tablet", ["read"], NOW)
    _, control = api_tokens.create_token(conn, "Home Assistant", ["control"], NOW)
    return SimpleNamespace(app=app, conn=conn, ids=ids, cast=fake_cast, clock=clock, config=config,
                           read=read, control=control)


def bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def client(api) -> TestClient:
    return TestClient(api.app)


@pytest.fixture
def reader(api) -> dict:
    return bearer(api.read)


@pytest.fixture
def controller(api) -> dict:
    return bearer(api.control)
