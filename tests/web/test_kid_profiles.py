"""Kid profiles (step 8, PR-1..PR-4): the profile list, groups on home/shows/play, per-profile KidState."""

import pytest
from fastapi.testclient import TestClient

from tellybox.web import kid
from tellybox.web.app import create_app


@pytest.fixture
def env(config, lib, fake_cast, tmp_path):
    conn, ids = lib
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>")
    conn.execute("UPDATE profile SET name = 'Mila', avatar = 'fox'")
    conn.execute("INSERT INTO profile (id, name, avatar, sort_order, created_at) VALUES (2, 'Noor', 'owl', 5, 'x')")
    conn.execute("INSERT INTO profile (id, name, picture_path, sort_order, created_at) VALUES (3, 'Sam', 'profiles/sam.jpg', 3, 'x')")
    (config.media_dir / "profiles").mkdir()
    (config.media_dir / "profiles" / "sam.jpg").write_bytes(b"JPEG:sam")
    app = create_app(config, conn=conn, cast=fake_cast, static_dir=static)
    return TestClient(app), conn, ids, fake_cast


def entry(pid, *, used=0, extra=0, unlimited=False, remaining=3600, can_start=True, reason=None, watching=False):
    return {"profile_id": pid, "day": "2026-09-28", "used_s": used, "extra_s": extra, "unlimited": unlimited,
            "blocked": reason == "blocked", "remaining_s": remaining, "can_start": can_start, "reason": reason,
            "watching": watching}


# --------------------------------------------------------------------------- GET /api/kid/profiles


def test_profiles_in_admin_order_with_picture_rules(env, mkstate):
    client, _, _, fake = env
    fake.current = mkstate(remaining_s=900, profiles=[
        entry(1, used=2700, remaining=900),
        entry(2, used=3600, remaining=0, can_start=False, reason="allowance"),
        entry(3, unlimited=True, remaining=None),
    ])
    r = client.get("/api/kid/profiles")
    assert r.status_code == 200
    assert r.json() == [
        {"profile_id": 1, "name": "Mila", "picture": None, "avatar": "fox", "ui_mode": "icons",
         "time_up": False, "fraction_left": 0.25, "last_five": False, "unlimited": False},
        {"profile_id": 3, "name": "Sam", "picture": "/img/profile/3.jpg", "avatar": None, "ui_mode": "icons",
         "time_up": False, "fraction_left": None, "last_five": False, "unlimited": True},
        {"profile_id": 2, "name": "Noor", "picture": None, "avatar": "owl", "ui_mode": "icons",
         "time_up": True, "fraction_left": 0.0, "last_five": True, "unlimited": False},
    ]


def test_profiles_carry_their_ui_mode(env):  # KA-11
    client, conn, *_ = env
    conn.execute("UPDATE profile SET ui_mode = 'text' WHERE id = 2")
    modes = {p["profile_id"]: p["ui_mode"] for p in client.get("/api/kid/profiles").json()}
    assert modes == {1: "icons", 3: "icons", 2: "text"}


def test_profiles_when_cast_is_down_still_lists_them(env):
    client, _, _, fake = env
    fake.mode = "down"
    r = client.get("/api/kid/profiles")
    assert r.status_code == 200
    assert [p["profile_id"] for p in r.json()] == [1, 3, 2]
    assert all(p["time_up"] is False for p in r.json())


def test_profile_photo(env):
    client, *_ = env
    r = client.get("/img/profile/3.jpg")
    assert r.status_code == 200 and r.content == b"JPEG:sam"
    assert r.headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("pid", [1, 999])
def test_profile_photo_404_without_photo_or_profile(env, pid):
    client, *_ = env
    assert client.get(f"/img/profile/{pid}.jpg").status_code == 404


@pytest.mark.parametrize("bad", ["../outside.jpg", "/etc/passwd"])
def test_profile_photo_outside_media_dir_is_404(env, bad):
    client, conn, *_ = env
    conn.execute("UPDATE profile SET picture_path = ? WHERE id = 3", (bad,))
    assert client.get("/img/profile/3.jpg").status_code == 404


def test_profile_photo_missing_file_is_404(env, config):
    client, *_ = env
    (config.media_dir / "profiles" / "sam.jpg").unlink()
    assert client.get("/img/profile/3.jpg").status_code == 404


# --------------------------------------------------------------------------- groups on home and shows


@pytest.mark.parametrize("group", ["", "x", "1,x", "1,999", "0", "-1", ",", "1,,2", ",".join(["1"] * 21)])
def test_bad_group_is_400(env, group):
    client, _, ids, _ = env
    for url in ("/api/kid/home", f"/api/kid/shows/{ids.bravo}"):
        r = client.get(url, params={"profiles": group})
        assert (r.status_code, r.json()) == (400, {"detail": "bad_profiles"}), (url, group)


def test_group_of_one_sees_only_their_own_continue_list(env, pos):
    client, _, ids, _ = env
    pos(ids.b1, 120, minutes=1, profile=1)
    pos(ids.a1, 60, minutes=2, profile=2)
    tiles = client.get("/api/kid/home", params={"profiles": "2"}).json()["continue"]
    assert [(t["kind"], t["episode_id"]) for t in tiles] == [("resume", ids.a1)]
    # omitting the group means the first profile in the admin's order (Mila, id 1)
    tiles = client.get("/api/kid/home").json()["continue"]
    assert [t["episode_id"] for t in tiles] == [ids.b1]


def test_group_continue_merges_by_recency_without_duplicates(env, pos):
    client, _, ids, _ = env
    pos(ids.b1, 120, minutes=1, profile=1)
    pos(ids.a1, 60, minutes=2, profile=2)
    pos(ids.b1, 300, minutes=3, profile=2)  # both watched b1; profile 2 most recently
    pos(ids.b2, 60, minutes=0, profile=1)
    tiles = client.get("/api/kid/home", params={"profiles": "1,2"}).json()["continue"]
    assert [(t["kind"], t["episode_id"]) for t in tiles] == [
        ("resume", ids.b1), ("resume", ids.a1), ("resume", ids.b2)]
    assert tiles[0]["progress"] == 0.5  # the most recent position among the group


def test_group_progress_is_the_most_recent_position(env, pos):
    client, _, ids, _ = env
    pos(ids.b1, 600, finished=True, minutes=1, profile=1)
    pos(ids.b1, 150, minutes=2, profile=2)
    pos(ids.b2, 300, minutes=5, profile=1)
    pos(ids.b2, 60, minutes=4, profile=2)
    body = client.get(f"/api/kid/shows/{ids.bravo}", params={"profiles": "2,1"}).json()
    eps = {e["episode_id"]: e for e in body["episodes"]}
    assert (eps[ids.b1]["progress"], eps[ids.b1]["finished"]) == (0.25, False)
    assert (eps[ids.b2]["progress"], eps[ids.b2]["finished"]) == (0.5, False)
    body = client.get(f"/api/kid/shows/{ids.bravo}", params={"profiles": "2"}).json()
    assert {e["episode_id"]: e for e in body["episodes"]}[ids.b2]["progress"] == 0.1


def test_group_continue_next_uses_the_most_recent_position_of_the_group(env, pos):
    client, _, ids, _ = env
    pos(ids.b1, 600, finished=True, minutes=1, profile=1)
    pos(ids.b1, 30, minutes=2, profile=2)  # rewatched by 2 and not finished: no "next" for the group
    tiles = client.get("/api/kid/home", params={"profiles": "1,2"}).json()["continue"]
    assert [(t["kind"], t["episode_id"]) for t in tiles] == [("resume", ids.b1)]
    tiles = client.get("/api/kid/home", params={"profiles": "1"}).json()["continue"]
    assert [(t["kind"], t["episode_id"]) for t in tiles] == [("next", ids.b2)]


def test_group_never_shows_hidden_content(env, pos):
    client, _, ids, _ = env
    pos(ids.h1, 100, profile=2)
    pos(ids.a3, 100, profile=2)
    assert client.get("/api/kid/home", params={"profiles": "1,2"}).json()["continue"] == []
    assert client.get(f"/api/kid/shows/{ids.hidden}", params={"profiles": "1,2"}).status_code == 404


def test_group_is_deduplicated(env, pos):
    client, _, ids, _ = env
    pos(ids.b1, 120, profile=1)
    tiles = client.get("/api/kid/home", params={"profiles": "1,1"}).json()["continue"]
    assert len(tiles) == 1


# --------------------------------------------------------------------------- play with a group


def test_play_passes_the_group_to_the_cast_service(env):
    client, _, ids, fake = env
    r = client.post("/api/kid/play", json={"episode_id": ids.a1, "profile_ids": [2, 1]})
    assert r.status_code == 200
    assert fake.calls == [("play", ids.a1, [2, 1])]
    assert r.json()["watching"] == [1, 2]


def test_play_without_group_means_the_first_profile(env):
    client, _, ids, fake = env
    assert client.post("/api/kid/play", json={"episode_id": ids.a1}).status_code == 200
    assert fake.calls == [("play", ids.a1, [1])]


@pytest.mark.parametrize("group", [[], [999], [1, 999], [1] * 21, [0]])
def test_play_bad_group_is_400_and_never_reaches_the_tv(env, group):
    client, _, ids, fake = env
    r = client.post("/api/kid/play", json={"episode_id": ids.a1, "profile_ids": group})
    assert (r.status_code, r.json()) == (400, {"detail": "bad_profiles"})
    assert fake.calls == []


def test_play_group_time_up_is_409(env):
    client, _, ids, fake = env
    fake.mode = "time_up"
    r = client.post("/api/kid/play", json={"episode_id": ids.a1, "profile_ids": [1, 2]})
    assert r.status_code == 409 and r.json()["time_up"] is True


# --------------------------------------------------------------------------- per-profile KidState


def test_kid_state_profiles_watching_and_day(env, mkstate, mkplaying):
    _, conn, ids, _ = env
    conn.execute("UPDATE profile SET daily_allowance_min = 60 WHERE id IN (1, 2)")
    profiles = [
        entry(1, used=3300, remaining=300, watching=True),
        entry(2, used=3600, extra=900, remaining=900, watching=True),
        entry(3, remaining=3600, can_start=False, reason="blocked"),
    ]
    s = kid.kid_state(conn, mkstate(remaining_s=300, profiles=profiles,
                                    now_playing=mkplaying(ids.a1, ids.alpha, profile_ids=[1, 2])))
    assert s["watching"] == [1, 2]
    assert s["day"] == "2026-09-28"
    assert s["profiles"]["1"] == {"fraction_left": round(300 / 3600, 3), "last_five": True, "unlimited": False,
                                  "time_up": False}
    assert s["profiles"]["2"] == {"fraction_left": 0.2, "last_five": False, "unlimited": False, "time_up": False}
    assert s["profiles"]["3"]["time_up"] is True
    assert set(s["profiles"]) == {"1", "2", "3"}
    # sky describes the current watchers: the lowest fraction of profiles 1 and 2 (3 isn't watching)
    assert s["sky"] == {"fraction_left": round(300 / 3600, 3), "last_five": True, "unlimited": False}


def test_kid_state_nothing_playing_has_no_watchers(lib, mkstate):
    conn, _ = lib
    assert kid.kid_state(conn, mkstate())["watching"] == []


def test_kid_state_profile_unlimited(lib, mkstate):
    conn, _ = lib
    s = kid.kid_state(conn, mkstate(remaining_s=None, unlimited=True))
    assert s["profiles"]["1"] == {"fraction_left": None, "last_five": False, "unlimited": True, "time_up": False}


def test_kid_state_survives_a_missing_timer(lib):
    conn, _ = lib
    s = kid.kid_state(conn, {"connection": "CONNECTED"})
    assert s["profiles"] == {"1": {"fraction_left": 1.0, "last_five": False, "unlimited": False, "time_up": False}}
    assert s["day"] is None and s["watching"] == []


def test_state_endpoint_includes_the_new_fields(env):
    client, *_ = env
    assert {"watching", "profiles", "day"} <= set(client.get("/api/kid/state").json())


def test_household_profile_id_is_gone():
    assert not hasattr(kid, "household_profile_id")
