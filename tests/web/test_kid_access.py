"""Show access in the kid API (PR-5..PR-8, KA-15, A-37): listing, show page, continue watching, picks."""

import pytest
from fastapi.testclient import TestClient

from tellybox import show_access
from tellybox.web.app import create_app
from tests.web.conftest import position
from tests.web.test_browser_label import IPHONE_SAFARI

pytestmark = pytest.mark.strict_access


@pytest.fixture
def env(config, lib, fake_cast, tmp_path):
    conn, ids = lib
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>")
    conn.execute("INSERT INTO profile (id, name, sort_order, created_at) VALUES (2, 'Noor', 5, 'x')")
    conn.execute("UPDATE profile SET watch_in_app = 1")
    app = create_app(config, conn=conn, cast=fake_cast, static_dir=static)
    return TestClient(app), conn, ids, fake_cast


def shows(client, profiles="1"):
    return [s["show_id"] for s in client.get(f"/api/kid/home?profiles={profiles}").json()["shows"]]


def test_a_profile_without_grants_sees_nothing(env):  # PR-5, KA-15
    client, conn, ids, _ = env
    home = client.get("/api/kid/home?profiles=1").json()
    assert home == {"continue": [], "shows": []}


def test_listing_shows_only_granted_shows(env):  # PR-5
    client, conn, ids, _ = env
    show_access.grant(conn, 1, ids.bravo)
    show_access.grant(conn, 2, ids.alpha)
    assert shows(client, "1") == [ids.bravo]
    assert shows(client, "2") == [ids.alpha]


def test_a_group_sees_the_intersection(env):  # PR-6
    client, conn, ids, _ = env
    show_access.set_profile_shows(conn, 1, [ids.bravo, ids.alpha])
    show_access.set_profile_shows(conn, 2, [ids.alpha])
    assert shows(client, "1,2") == [ids.alpha]
    show_access.set_profile_shows(conn, 2, [])
    assert shows(client, "1,2") == []  # KA-15: an empty intersection


def test_show_page_refuses_a_hidden_show(env):  # PR-7
    client, conn, ids, _ = env
    show_access.grant(conn, 1, ids.bravo)
    assert client.get(f"/api/kid/shows/{ids.bravo}?profiles=1").status_code == 200
    assert client.get(f"/api/kid/shows/{ids.alpha}?profiles=1").status_code == 404
    assert client.get(f"/api/kid/shows/{ids.bravo}?profiles=1,2").status_code == 404  # 2 lacks it


def test_continue_watching_drops_revoked_shows_and_keeps_positions(env):  # PR-8
    client, conn, ids, _ = env
    show_access.grant(conn, 1, ids.bravo)
    position(conn, ids.b1, 120)
    assert [t["episode_id"] for t in client.get("/api/kid/home?profiles=1").json()["continue"]] == [ids.b1]
    show_access.revoke(conn, 1, ids.bravo)
    assert client.get("/api/kid/home?profiles=1").json()["continue"] == []
    assert conn.execute("SELECT position_s FROM playback_position WHERE episode_id = ?", (ids.b1,)).fetchone()[0] == 120
    show_access.grant(conn, 1, ids.bravo)  # re-grant restores resume
    assert [t["episode_id"] for t in client.get("/api/kid/home?profiles=1").json()["continue"]] == [ids.b1]


def test_continue_next_episode_needs_access_too(env):  # PR-8, PB-4
    client, conn, ids, _ = env
    position(conn, ids.b1, 600, finished=True)
    assert client.get("/api/kid/home?profiles=1").json()["continue"] == []


def test_tv_pick_is_refused_for_a_hidden_show_and_never_reaches_the_cast(env):  # PR-7
    client, conn, ids, fake = env
    show_access.grant(conn, 1, ids.bravo)
    r = client.post("/api/kid/play", json={"episode_id": ids.a1, "profile_ids": [1]})
    assert r.status_code == 404
    assert not [c for c in fake.calls if c[0] == "play"]
    ok = client.post("/api/kid/play", json={"episode_id": ids.b1, "profile_ids": [1]})
    assert ok.status_code == 200
    r = client.post("/api/kid/play", json={"episode_id": ids.b1, "profile_ids": [1, 2]})
    assert r.status_code == 404  # a group needs every member


def test_device_pick_is_refused_for_a_hidden_show(env):  # PR-7, PB-7
    client, conn, ids, fake = env
    show_access.grant(conn, 1, ids.bravo)
    body = {"episode_id": ids.a1, "profile_ids": [1], "target": "device", "device_id": "device-aaaa1111"}
    r = client.post("/api/kid/play", json=body, headers={"User-Agent": IPHONE_SAFARI})
    assert r.status_code == 404
    assert not [c for c in fake.calls if c[0] == "device_play"]
    r = client.post("/api/kid/play", json={**body, "episode_id": ids.b1}, headers={"User-Agent": IPHONE_SAFARI})
    assert r.status_code == 200
