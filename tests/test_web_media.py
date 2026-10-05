"""Signed media route (NF-3): the Chromecast fetches episodes with Range requests."""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tellybox import db, library, store
from tellybox.clock import FakeClock
from tellybox.config import Config
from tellybox.media_urls import media_path
from tellybox.web.app import create_app

SECRET = b"test-secret"
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
PAYLOAD = bytes(range(256)) * 40  # 10240 bytes


def make_config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "data" / "tellybox.db",
        media_dir=tmp_path / "media",
        data_dir=tmp_path / "data",
        tz=ZoneInfo("Europe/Amsterdam"),
        web_host="127.0.0.1",
        web_port=8080,
        cast_api_host="127.0.0.1",
        cast_api_port=8081,
        media_base_url="http://127.0.0.1:8080",
        secret=SECRET,
    )


@pytest.fixture
def env(tmp_path):
    config = make_config(tmp_path)
    (config.media_dir / "show").mkdir(parents=True)
    (config.media_dir / "show" / "ep1.mp4").write_bytes(PAYLOAD)
    (tmp_path / "outside.mp4").write_bytes(b"secret stuff")
    conn = db.open_db(":memory:")
    sid = library.create_show(conn, "Show", now=NOW)
    ep = library.add_episode(conn, sid, "Ep 1", "show/ep1.mp4", now=NOW)
    clock = FakeClock(NOW)
    client = TestClient(create_app(config, conn=conn, clock=clock))
    yield client, conn, clock, sid, ep
    conn.close()


def url_for(ep: int, expires: datetime = NOW + timedelta(hours=1)) -> str:
    return media_path(SECRET, ep, int(expires.timestamp()))


def test_healthz(env):
    client, *_ = env
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "dev"}


def test_healthz_reports_configured_version(tmp_path):
    config = dataclasses.replace(make_config(tmp_path), version="2026.09.28.7")
    client = TestClient(create_app(config, clock=FakeClock(NOW)))
    assert client.get("/healthz").json() == {"ok": True, "version": "2026.09.28.7"}


def test_serves_full_file(env):
    client, _, _, _, ep = env
    r = client.get(url_for(ep))
    assert r.status_code == 200
    assert r.content == PAYLOAD
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"


def test_range_request_returns_206(env):
    client, _, _, _, ep = env
    r = client.get(url_for(ep), headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == PAYLOAD[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(PAYLOAD)}"


def test_head(env):
    client, _, _, _, ep = env
    r = client.head(url_for(ep))
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(PAYLOAD))
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == b""


def test_rejects_bad_signature(env):
    client, _, _, _, ep = env
    path = url_for(ep)
    bad = path[:-5] + ("A" if path[-5] != "A" else "B") + ".mp4"
    assert client.get(bad).status_code == 404
    assert client.head(bad).status_code == 404


def test_rejects_expired(env):
    client, _, clock, _, ep = env
    path = url_for(ep, NOW + timedelta(minutes=5))
    assert client.get(path).status_code == 200
    clock.advance(minutes=6)
    assert client.get(path).status_code == 404


def test_unknown_episode(env):
    client, *_ = env
    assert client.get(url_for(999)).status_code == 404


def test_missing_file(env):
    client, conn, _, sid, _ = env
    ep = library.add_episode(conn, sid, "Gone", "show/missing.mp4", now=NOW)
    assert client.get(url_for(ep)).status_code == 404


@pytest.mark.parametrize("bad_path", ["../outside.mp4", "show/../../outside.mp4", "/etc/passwd"])
def test_rejects_path_traversal(env, bad_path):
    client, conn, _, sid, _ = env
    ep = library.add_episode(conn, sid, "Evil", bad_path, now=NOW)
    assert client.get(url_for(ep)).status_code == 404


def test_rejects_symlink_out_of_media_dir(env, tmp_path):
    client, conn, _, sid, _ = env
    (tmp_path / "media" / "show" / "link.mp4").symlink_to(tmp_path / "outside.mp4")
    ep = library.add_episode(conn, sid, "Link", "show/link.mp4", now=NOW)
    assert client.get(url_for(ep)).status_code == 404


def test_hidden_episode_still_served(env):
    client, conn, _, _, ep = env
    conn.execute("UPDATE episode SET hidden = 1 WHERE id = ?", (ep,))
    assert client.get(url_for(ep)).status_code == 200


def test_opens_db_from_config_when_no_conn(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config, clock=FakeClock(NOW))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert config.db_path.exists()


# --------------------------------------------------------------------------- in-app URLs (PB-7, WT-11)


def device_session(conn, ep: int, target: str = "device") -> int:
    return store.open_watch_session(conn, ep, [1], NOW, target=target, device_label="iPhone Safari")


def scoped_url(ep: int, session_id: int, expires: datetime = NOW + timedelta(hours=1)) -> str:
    return media_path(SECRET, ep, int(expires.timestamp()), session_id)


def test_scoped_url_is_served_while_the_session_is_open(env):
    client, conn, _, _, ep = env
    r = client.get(scoped_url(ep, device_session(conn, ep)))
    assert r.status_code == 200 and r.content == PAYLOAD


def test_scoped_url_supports_range_requests(env):
    client, conn, _, _, ep = env
    r = client.get(scoped_url(ep, device_session(conn, ep)), headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == PAYLOAD[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(PAYLOAD)}"


def test_scoped_url_dies_with_its_session(env):
    client, conn, _, _, ep = env
    session_id = device_session(conn, ep)
    store.close_watch_session(conn, session_id, "stopped", NOW)
    assert client.get(scoped_url(ep, session_id)).status_code == 403


def test_scoped_url_for_another_episode_is_refused(env):
    client, conn, _, sid, ep = env
    other = library.add_episode(conn, sid, "Ep 2", "show/ep1.mp4", now=NOW)
    session_id = device_session(conn, other)
    assert client.get(scoped_url(ep, session_id)).status_code == 403


def test_scoped_url_needs_a_device_session(env):
    client, conn, _, _, ep = env
    assert client.get(scoped_url(ep, device_session(conn, ep, target="tv"))).status_code == 403
    assert client.get(scoped_url(ep, 9999)).status_code == 403  # no such session


def test_scoped_url_with_a_forged_session_id_is_refused(env):
    client, conn, _, _, ep = env
    session_id = device_session(conn, ep)
    other = device_session(conn, ep)
    assert client.get(url_for(ep) + f"?s={session_id}").status_code == 403  # unscoped signature
    forged = scoped_url(ep, session_id).replace(f"?s={session_id}", f"?s={other}")
    assert client.get(forged).status_code == 403
    assert client.get(url_for(ep) + "?s=abc").status_code == 403


def test_expired_scoped_url_is_refused(env):
    client, conn, _, _, ep = env
    session_id = device_session(conn, ep)
    assert client.get(scoped_url(ep, session_id, NOW - timedelta(seconds=1))).status_code == 403


def test_url_without_s_is_unchanged_by_open_sessions(env):
    client, conn, _, _, ep = env
    device_session(conn, ep)
    assert client.get(url_for(ep)).status_code == 200


def test_scoped_url_keeps_serving_after_the_show_is_revoked(env):  # PR-8: the episode in progress finishes
    client, conn, _, sid, ep = env
    session_id = device_session(conn, ep)
    conn.execute("DELETE FROM profile_show WHERE show_id = ?", (sid,))
    r = client.get(scoped_url(ep, session_id), headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
