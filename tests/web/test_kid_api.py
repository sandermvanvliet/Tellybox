"""Kid app API (docs/kid-api.md): KA-3..KA-9, NF-1, PB-4."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tellybox import library
from tellybox.web import app as web_app
from tellybox.web import kid
from tellybox.web.app import create_app

TILE_KEYS = {"episode_id", "show_id", "thumb", "title", "progress", "finished"}


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "static"
    (d / "js").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>Tellybox</title>")
    (d / "manifest.webmanifest").write_text('{"name": "Tellybox"}')
    (d / "favicon.ico").write_bytes(b"ico")
    (d / "js" / "app.js").write_text("console.log('hi')")
    return d


@pytest.fixture
def env(config, lib, fake_cast, static_dir):
    conn, ids = lib
    app = create_app(config, conn=conn, cast=fake_cast, static_dir=static_dir)
    return TestClient(app), conn, ids, fake_cast, app


# --------------------------------------------------------------------------- home & shows (KA-3, KA-4)


def test_home_lists_visible_shows_with_visible_episodes_in_order(env):
    client, _, ids, *_ = env
    r = client.get("/api/kid/home")
    assert r.status_code == 200
    assert r.json()["shows"] == [
        {"show_id": ids.bravo, "artwork": f"/img/show/{ids.bravo}.jpg", "title": "Bravo"},
        {"show_id": ids.alpha, "artwork": f"/img/show/{ids.alpha}.jpg", "title": "Alpha"},
    ]
    assert r.json()["continue"] == []


def test_show_sort_order_ties_break_on_id(env):
    client, conn, ids, *_ = env
    conn.execute("UPDATE show SET sort_order = 0")
    assert [s["show_id"] for s in client.get("/api/kid/home").json()["shows"]] == [ids.alpha, ids.bravo]


def test_show_page_lists_visible_episodes_in_order(env):
    client, conn, ids, *_ = env
    conn.execute("UPDATE episode SET sort_order = 0 WHERE id = ?", (ids.a4,))  # ties on sort_order break on id
    r = client.get(f"/api/kid/shows/{ids.alpha}")
    assert r.status_code == 200
    body = r.json()
    assert {k: body[k] for k in ("show_id", "artwork", "title")} == {
        "show_id": ids.alpha, "artwork": f"/img/show/{ids.alpha}.jpg", "title": "Alpha"}
    assert [e["episode_id"] for e in body["episodes"]] == [ids.a1, ids.a4, ids.a2]
    assert body["episodes"][0] == {"episode_id": ids.a1, "show_id": ids.alpha, "thumb": f"/img/episode/{ids.a1}.jpg",
                                   "title": "Title a1", "progress": None, "finished": False}


def test_show_page_progress_and_finished(env, pos):
    client, _, ids, *_ = env
    pos(ids.b1, 600, finished=True)
    pos(ids.b2, 150)
    pos(ids.b3, 30)  # unknown duration
    eps = {e["episode_id"]: e for e in client.get(f"/api/kid/shows/{ids.bravo}").json()["episodes"]}
    assert (eps[ids.b1]["progress"], eps[ids.b1]["finished"]) == (1.0, True)
    assert (eps[ids.b2]["progress"], eps[ids.b2]["finished"]) == (0.25, False)
    assert (eps[ids.b3]["progress"], eps[ids.b3]["finished"]) == (None, False)


def test_progress_is_per_profile(lib, pos):
    conn, ids = lib
    conn.execute("INSERT INTO profile (id, name, sort_order, created_at) VALUES (2, 'Other', 2, '2026-01-01T00:00:00Z')")
    pos(ids.b1, 300, profile=2)
    assert kid.show_episodes(conn, ids.bravo, profile_ids=[2])[0]["progress"] == 0.5
    assert kid.show_episodes(conn, ids.bravo, profile_ids=[1])[0]["progress"] is None
    assert kid.show_episodes(conn, ids.bravo)[0]["progress"] is None  # the first profile by default


@pytest.mark.parametrize("which", ["hidden", "missing"])
def test_show_page_404(env, which):
    client, _, ids, *_ = env
    show_id = ids.hidden if which == "hidden" else 999
    r = client.get(f"/api/kid/shows/{show_id}")
    assert r.status_code == 404


def test_hiding_takes_effect_immediately(env):
    client, conn, ids, *_ = env
    conn.execute("UPDATE episode SET hidden = 1 WHERE show_id = ?", (ids.bravo,))
    assert [s["show_id"] for s in client.get("/api/kid/home").json()["shows"]] == [ids.alpha]
    conn.execute("UPDATE show SET hidden = 1 WHERE id = ?", (ids.alpha,))
    assert client.get("/api/kid/home").json()["shows"] == []
    assert client.get(f"/api/kid/shows/{ids.alpha}").status_code == 404


# --------------------------------------------------------------------------- continue watching (KA-3, PB-4)


def cont(client):
    return [(t["kind"], t["episode_id"]) for t in client.get("/api/kid/home").json()["continue"]]


def test_continue_watching_resume_and_next(env, pos):
    client, _, ids, *_ = env
    pos(ids.a1, 600, finished=True, minutes=1)
    pos(ids.b1, 120, minutes=2)
    pos(ids.a2, 590, finished=True, minutes=3)  # next skips hidden a3 -> a4
    pos(ids.b2, 5, minutes=4)  # barely started: not "resume"
    pos(ids.h1, 100, minutes=5)  # hidden show
    pos(ids.a3, 100, minutes=0)  # hidden episode
    assert cont(client) == [("next", ids.a4), ("resume", ids.b1)]
    tile = client.get("/api/kid/home").json()["continue"][0]
    assert set(tile) == TILE_KEYS | {"kind"}
    assert tile["thumb"] == f"/img/episode/{ids.a4}.jpg" and tile["progress"] is None


def test_continue_next_only_when_latest_of_show_is_finished(env, pos):
    client, _, ids, *_ = env
    pos(ids.a1, 600, finished=True, minutes=1)
    pos(ids.a2, 60, minutes=2)
    assert cont(client) == [("resume", ids.a2)]


def test_continue_next_not_duplicated_when_already_resumable(env, pos):
    client, _, ids, *_ = env
    pos(ids.b2, 60, minutes=1)
    pos(ids.b1, 600, finished=True, minutes=2)  # rewatched b1 after starting b2
    assert cont(client) == [("resume", ids.b2)]


def test_continue_no_next_after_last_episode(env, pos):
    client, _, ids, *_ = env
    pos(ids.a4, 600, finished=True)
    assert cont(client) == []


def test_continue_next_skips_hidden_show_and_hidden_next(env, pos):
    client, conn, ids, *_ = env
    pos(ids.h1, 600, finished=True)
    conn.execute("UPDATE episode SET hidden = 1 WHERE id = ?", (ids.b2,))
    pos(ids.b1, 600, finished=True, minutes=1)
    assert cont(client) == [("next", ids.b3)]


def test_continue_capped_at_eight(env, pos):
    client, conn, ids, *_ = env
    eps = [library.add_episode(conn, ids.bravo, f"x{i}", f"x{i}.mp4", now=datetime(2026, 1, 1, tzinfo=UTC), duration_s=600)
           for i in range(10)]
    for i, e in enumerate(eps):
        pos(e, 60, minutes=i)
    assert cont(client) == [("resume", e) for e in reversed(eps)][:8]


# --------------------------------------------------------------------------- state reduction (KA-6..KA-9)


def test_reduce_connected(lib, mkstate, mkplaying):
    conn, ids = lib
    s = kid.kid_state(conn, mkstate(remaining_s=1800, now_playing=mkplaying(ids.a1, ids.alpha, "paused", "Title a1")))
    assert s == {
        "tv": "ok",
        "now_playing": {"episode_id": ids.a1, "show_id": ids.alpha, "thumb": f"/img/episode/{ids.a1}.jpg",
                        "title": "Title a1", "state": "paused"},
        "sky": {"fraction_left": 0.5, "last_five": False, "unlimited": False},
        "time_up": False,
        "watching": [1],
        "profiles": {"1": {"fraction_left": 0.5, "last_five": False, "unlimited": False, "time_up": False}},
        "day": "2026-09-28",
    }


@pytest.mark.parametrize("connection", ["CONNECTING", "LOST", "FAILED", "DISCONNECTED", "NO_DEVICE"])
def test_reduce_not_connected_is_unreachable(lib, mkstate, connection):
    conn, _ = lib
    assert kid.kid_state(conn, mkstate(connection=connection))["tv"] == "unreachable"


@pytest.mark.parametrize("cast, kid_state", [
    ("loading", "loading"), ("playing", "playing"), ("paused", "paused"), ("buffering", "buffering"),
    ("idle", "loading"), ("weird", "loading"),
])
def test_reduce_player_states(lib, mkstate, mkplaying, cast, kid_state):
    conn, ids = lib
    s = kid.kid_state(conn, mkstate(now_playing=mkplaying(ids.a1, ids.alpha, cast)))
    assert s["now_playing"]["state"] == kid_state


def test_reduce_now_playing_leaks_nothing_else(lib, mkstate, mkplaying):
    conn, ids = lib
    s = kid.kid_state(conn, mkstate(now_playing=mkplaying(ids.a1, ids.alpha)))
    assert set(s) == {"tv", "now_playing", "sky", "time_up", "watching", "profiles", "day"}
    assert set(s["now_playing"]) == {"episode_id", "show_id", "thumb", "title", "state"}


def test_reduce_unlimited(lib, mkstate):
    conn, _ = lib
    s = kid.kid_state(conn, mkstate(remaining_s=None, unlimited=True))
    assert s["sky"] == {"fraction_left": None, "last_five": False, "unlimited": True}


@pytest.mark.parametrize("remaining, last_five", [(301, False), (300, True), (0, True)])
def test_reduce_last_five(lib, mkstate, remaining, last_five):
    conn, _ = lib
    assert kid.kid_state(conn, mkstate(remaining_s=remaining))["sky"]["last_five"] is last_five


def test_reduce_fraction_includes_extra_minutes(lib, mkstate):
    conn, _ = lib
    s = kid.kid_state(conn, mkstate(remaining_s=1800, used_s=2700, extra_s=900))
    assert s["sky"]["fraction_left"] == 0.4  # 1800 / (3600 + 900)


def test_reduce_fraction_uses_db_allowance_and_clamps(lib, mkstate):
    conn, _ = lib
    conn.execute("UPDATE profile SET daily_allowance_min = 30")
    assert kid.kid_state(conn, mkstate(remaining_s=900))["sky"]["fraction_left"] == 0.5
    assert kid.kid_state(conn, mkstate(remaining_s=5000))["sky"]["fraction_left"] == 1.0
    conn.execute("UPDATE profile SET daily_allowance_min = 0")
    assert kid.kid_state(conn, mkstate(remaining_s=0))["sky"]["fraction_left"] == 0.0


def test_reduce_several_profiles_uses_minimum_fraction(lib, mkstate):
    conn, _ = lib
    conn.execute("INSERT INTO profile (id, name, daily_allowance_min, created_at) VALUES (2, 'B', 120, 'x')")
    profiles = [
        {"profile_id": 1, "used_s": 1800, "extra_s": 0, "unlimited": False},   # 0.5 left
        {"profile_id": 2, "used_s": 1800, "extra_s": 0, "unlimited": False},   # 0.75 left
    ]
    assert kid.kid_state(conn, mkstate(remaining_s=1800, profiles=profiles))["sky"]["fraction_left"] == 0.5
    profiles[0]["unlimited"] = True
    assert kid.kid_state(conn, mkstate(remaining_s=5400, profiles=profiles))["sky"]["fraction_left"] == 0.75


def test_reduce_time_up(lib, mkstate):
    conn, _ = lib
    assert kid.kid_state(conn, mkstate(remaining_s=0, time_up=True))["time_up"] is True


# --------------------------------------------------------------------------- state & commands


def test_state_endpoint(env, mkstate):
    client, _, _, fake, _ = env
    fake.current = mkstate(remaining_s=900)
    r = client.get("/api/kid/state")
    assert r.status_code == 200
    assert r.json()["tv"] == "ok"
    assert r.json()["sky"]["fraction_left"] == 0.25


def test_state_endpoint_unreachable(env):
    client, _, _, fake, _ = env
    fake.mode = "down"
    r = client.get("/api/kid/state")
    assert r.status_code == 200
    assert r.json()["tv"] == "unreachable" and r.json()["now_playing"] is None


def test_play(env):
    client, _, ids, fake, _ = env
    r = client.post("/api/kid/play", json={"episode_id": ids.a1})
    assert r.status_code == 200
    assert r.json()["now_playing"]["episode_id"] == ids.a1
    assert r.json()["now_playing"]["state"] == "loading"
    assert fake.calls == [("play", ids.a1, [1])]


@pytest.mark.parametrize("which", ["a3", "h1", "e1", "missing"])
def test_play_invisible_is_404_and_never_reaches_the_tv(env, which):
    client, _, ids, fake, _ = env
    episode_id = getattr(ids, which, 999)
    r = client.post("/api/kid/play", json={"episode_id": episode_id})
    assert r.status_code == 404
    assert r.json() == {"detail": "not_found"}
    assert fake.calls == []


def test_play_cast_does_not_know_episode(env):
    client, _, ids, fake, _ = env
    fake.mode = "not_found"
    r = client.post("/api/kid/play", json={"episode_id": ids.a1})
    assert (r.status_code, r.json()) == (404, {"detail": "not_found"})


def test_play_time_up_is_409_kid_state(env):
    client, _, ids, fake, _ = env
    fake.mode = "time_up"
    r = client.post("/api/kid/play", json={"episode_id": ids.a1})
    assert r.status_code == 409
    assert r.json()["time_up"] is True
    assert r.json()["sky"]["fraction_left"] == 0.0


def test_play_unreachable_is_503_kid_state(env):
    client, _, ids, fake, app = env
    fake.mode = "down"
    app.state.hub.publish({**app.state.hub.state, "tv": "ok", "time_up": True})
    r = client.post("/api/kid/play", json={"episode_id": ids.a1})
    assert r.status_code == 503
    assert r.json()["tv"] == "unreachable"
    assert r.json()["time_up"] is True  # last known


def test_play_rejects_bad_body(env):
    client, *_ = env
    assert client.post("/api/kid/play", json={}).status_code == 422


@pytest.mark.parametrize("command", ["pause", "resume"])
def test_pause_resume(env, command):
    client, _, _, fake, _ = env
    r = client.post(f"/api/kid/{command}")
    assert r.status_code == 200
    assert r.json()["tv"] == "ok"
    assert fake.calls == [(command,)]
    fake.mode = "down"
    r = client.post(f"/api/kid/{command}")
    assert r.status_code == 503
    assert r.json()["tv"] == "unreachable"


# --------------------------------------------------------------------------- images (KA-2, KA-4)


def test_episode_image(env):
    client, _, ids, *_ = env
    r = client.get(f"/img/episode/{ids.a1}.jpg")
    assert r.status_code == 200
    assert r.content == b"JPEG:thumbs/a1.jpg"
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "max-age=3600"


@pytest.mark.parametrize("which", ["a3", "h1", "e1", "missing"])
def test_episode_image_invisible_is_404(env, which):
    client, _, ids, *_ = env
    assert client.get(f"/img/episode/{getattr(ids, which, 999)}.jpg").status_code == 404


def test_episode_image_without_thumbnail_or_file_is_404(env, config):
    client, conn, ids, *_ = env
    conn.execute("UPDATE episode SET thumbnail_path = NULL WHERE id = ?", (ids.a1,))
    assert client.get(f"/img/episode/{ids.a1}.jpg").status_code == 404
    (config.media_dir / "thumbs" / "a2.jpg").unlink()
    assert client.get(f"/img/episode/{ids.a2}.jpg").status_code == 404


@pytest.mark.parametrize("bad", ["../outside.jpg", "/etc/passwd"])
def test_episode_image_outside_media_dir_is_404(env, bad):
    client, conn, ids, *_ = env
    conn.execute("UPDATE episode SET thumbnail_path = ? WHERE id = ?", (bad, ids.a1))
    assert client.get(f"/img/episode/{ids.a1}.jpg").status_code == 404


def test_show_image_artwork(env):
    client, _, ids, *_ = env
    r = client.get(f"/img/show/{ids.bravo}.jpg")
    assert r.status_code == 200 and r.content == b"JPEG:art/bravo.jpg"
    assert r.headers["cache-control"] == "max-age=3600"


def test_show_image_falls_back_to_first_visible_episode_thumbnail(env):
    client, conn, ids, *_ = env
    assert client.get(f"/img/show/{ids.alpha}.jpg").content == b"JPEG:thumbs/a1.jpg"
    conn.execute("UPDATE episode SET hidden = 1 WHERE id = ?", (ids.a1,))
    assert client.get(f"/img/show/{ids.alpha}.jpg").content == b"JPEG:thumbs/a2.jpg"
    conn.execute("UPDATE show SET artwork_path = 'art/missing.jpg' WHERE id = ?", (ids.alpha,))
    assert client.get(f"/img/show/{ids.alpha}.jpg").content == b"JPEG:thumbs/a2.jpg"


@pytest.mark.parametrize("which", ["hidden", "empty", "missing"])
def test_show_image_404(env, which):
    client, _, ids, *_ = env
    assert client.get(f"/img/show/{getattr(ids, which, 999)}.jpg").status_code == 404


def test_show_image_nothing_available_is_404(env):
    client, conn, ids, *_ = env
    conn.execute("UPDATE episode SET thumbnail_path = NULL WHERE show_id = ?", (ids.alpha,))
    assert client.get(f"/img/show/{ids.alpha}.jpg").status_code == 404


# --------------------------------------------------------------------------- shell & static


def test_index(env):
    client, *_ = env
    r = client.get("/")
    assert r.status_code == 200
    assert "Tellybox" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_index_missing_is_404(config, lib, fake_cast, tmp_path, caplog):
    conn, _ = lib
    client = TestClient(create_app(config, conn=conn, cast=fake_cast, static_dir=tmp_path / "nope"))
    assert client.get("/").status_code == 404
    assert "index.html" in caplog.text


def test_manifest_and_static(env):
    client, *_ = env
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/manifest+json"
    assert r.json() == {"name": "Tellybox"}
    assert client.get("/static/js/app.js").text == "console.log('hi')"
    assert client.get("/static/nope.js").status_code == 404


def test_favicon_ico_at_the_root(env):
    client, *_ = env
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.content == b"ico"
    assert r.headers["content-type"] == "image/vnd.microsoft.icon"


def test_shipped_shell_links_the_icons():
    index = (web_app.STATIC_DIR / "index.html").read_text()
    assert 'href="/static/favicon.svg"' in index
    assert 'href="/favicon.ico"' in index
    manifest = json.loads((web_app.STATIC_DIR / "manifest.webmanifest").read_text())
    for name in ["favicon.svg", "favicon.ico", *(Path(i["src"]).name for i in manifest["icons"])]:
        assert (web_app.STATIC_DIR / name).is_file(), name


def test_default_static_dir_is_the_package():
    assert web_app.STATIC_DIR == Path(web_app.__file__).parent / "static"


# --------------------------------------------------------------------------- live updates (KA-7)


async def test_events_endpoint_sends_first_event_immediately(env):
    _, _, _, _, app = env
    hub = app.state.hub
    sent: list[dict] = []
    got_event = asyncio.Event()
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body", b"").startswith(b"data:"):
            got_event.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/api/kid/events", "raw_path": b"/api/kid/events", "root_path": "", "query_string": b"",
             "headers": [], "server": ("test", 80), "client": ("test", 1234)}
    task = asyncio.create_task(app(scope, receive, send))
    try:
        async with asyncio.timeout(2):
            await got_event.wait()
    finally:
        disconnect.set()
        async with asyncio.timeout(2):
            await task
    start = sent[0]
    assert start["status"] == 200
    assert (b"content-type", b"text/event-stream; charset=utf-8") in start["headers"]
    body = next(m["body"] for m in sent if m.get("body", b"").startswith(b"data:"))
    assert json.loads(body[6:]) == hub.state
    assert not hub._subscribers


def test_lifespan_runs_the_hub(config, lib, fake_cast, static_dir, mkstate):
    conn, _ = lib
    fake_cast.streams = [[mkstate(remaining_s=900)]]
    app = create_app(config, conn=conn, cast=fake_cast, static_dir=static_dir)
    with TestClient(app):
        for _ in range(200):
            if app.state.hub.state["sky"]["fraction_left"] == 0.25:
                break
            time.sleep(0.01)
    # the scripted stream ended, so the TV is unreachable but the last known sky is kept
    assert app.state.hub.state["sky"]["fraction_left"] == 0.25
