"""GET /api/admin/state and the AdminState shape (HA-2), in absolute units."""

from __future__ import annotations

from tellybox import api_tokens, jobs, library
from tellybox.jobs import JobStatus
from tellybox.web.api.state import LAST_FIVE_S
from tests.web.conftest import NOW, cast_state, playing

STATE = "/api/admin/state"


def profile_state(pid, *, used_s=600, extra_s=0, remaining_s=1200, unlimited=False, blocked=False, watching=False,
                  can_start=True, reason=None, session_elapsed_s=None):
    return {"profile_id": pid, "day": "2026-09-28", "used_s": used_s, "extra_s": extra_s, "unlimited": unlimited,
            "blocked": blocked, "remaining_s": remaining_s, "can_start": can_start, "reason": reason,
            "session_elapsed_s": session_elapsed_s, "watching": watching}


def two_profiles(api, **first):
    api.cast.current = cast_state(profiles=[profile_state(1, **first), profile_state(2, remaining_s=250, used_s=1550)])


def test_state_shape_and_absolute_units(client, api, reader):
    two_profiles(api, used_s=2710, extra_s=900, remaining_s=1790, watching=True, session_elapsed_s=1200)
    np = playing(b := api.ids.b1, api.ids.bravo, "playing", "Bravo one", [1])
    state = cast_state(now_playing=np, remaining_s=1790, profiles=api.cast.current["timer"]["profiles"])
    state["timer"]["session_elapsed_s"] = 1200
    state["timer"]["session_started_at"] = "2026-09-28T13:40:00+00:00"
    api.cast.current = state
    body = client.get(STATE, headers=reader).json()

    assert body["instance_id"] == api_tokens.instance_id(api.conn)
    assert body["version"] == api.config.version and body["api"] == 1
    assert body["day"] == {"date": "2026-09-28", "resets_at": "2026-09-29T02:00:00+00:00"}
    assert body["tv"] == {"connection": "CONNECTED", "reachable": True, "device": "Living Room TV"}
    assert body["now_playing"] == {
        "episode_id": b, "show_id": api.ids.bravo, "title": "Bravo one", "show": "Bravo", "state": "playing",
        "position_s": 12, "duration_s": 600, "profile_ids": [1]}
    assert body["group"] == {
        "remaining_s": 1790, "time_up": False, "last_five": False, "action": "continue", "reason": None,
        "grace_ends_at": None, "session_started_at": "2026-09-28T13:40:00+00:00", "session_elapsed_s": 1200}
    mila, noor = body["profiles"]
    assert mila == {
        "id": 1, "name": "Mila", "avatar": "fox", "allowance_s": 3600, "extra_s": 900, "used_s": 2710,
        "remaining_s": 1790, "unlimited": False, "blocked": False, "mode": "ignore_pauses", "max_session_s": 5400,
        "session_elapsed_s": 1200, "can_start": True, "reason": None, "watching": True, "last_five": False}
    assert noor["id"] == 2 and noor["name"] == "Noor" and noor["avatar"] is None
    assert noor["allowance_s"] == 1800 and noor["mode"] == "wall_clock" and noor["max_session_s"] == 2700
    assert noor["remaining_s"] == 250 and noor["last_five"] is True and noor["watching"] is False


def test_profiles_follow_the_admin_order(client, api, reader):
    api.conn.execute("UPDATE profile SET sort_order = 5 WHERE id = 1")
    two_profiles(api)
    assert [p["id"] for p in client.get(STATE, headers=reader).json()["profiles"]] == [2, 1]


def test_last_five_boundary(client, api, reader):
    assert LAST_FIVE_S == 300
    api.cast.current = cast_state(remaining_s=300, profiles=[
        profile_state(1, remaining_s=300), profile_state(2, remaining_s=301)])
    body = client.get(STATE, headers=reader).json()
    assert [p["last_five"] for p in body["profiles"]] == [True, False]
    assert body["group"]["last_five"] is True


def test_unlimited_profile_has_null_remaining_and_no_last_five(client, api, reader):
    api.cast.current = cast_state(remaining_s=None, unlimited=True, profiles=[
        profile_state(1, unlimited=True, remaining_s=None), profile_state(2)])
    body = client.get(STATE, headers=reader).json()
    assert body["profiles"][0]["remaining_s"] is None and body["profiles"][0]["last_five"] is False
    assert body["group"]["remaining_s"] is None and body["group"]["last_five"] is False


def test_time_up_group(client, api, reader):
    state = cast_state(remaining_s=0, time_up=True, profiles=[
        profile_state(1, remaining_s=0, can_start=False, reason="allowance"), profile_state(2)])
    state["timer"].update(action="finish_then_stop", reason="allowance", grace_deadline="2026-09-28T14:10:00+00:00")
    api.cast.current = state
    group = client.get(STATE, headers=reader).json()["group"]
    assert group["time_up"] is True and group["action"] == "finish_then_stop" and group["reason"] == "allowance"
    assert group["grace_ends_at"] == "2026-09-28T14:10:00+00:00"


def test_missing_new_cast_fields_are_tolerated(client, api, reader):
    state = cast_state(profiles=[profile_state(1), profile_state(2)])
    del state["timer"]["next_reset"]
    for p in state["timer"]["profiles"]:
        del p["session_elapsed_s"]
    api.cast.current = state
    body = client.get(STATE, headers=reader).json()
    assert body["day"]["resets_at"] is None
    assert body["profiles"][0]["session_elapsed_s"] is None


def test_unreachable_keeps_last_known_profiles_and_drops_now_playing(client, api, reader):
    np = playing(api.ids.b1, api.ids.bravo, "playing", "x", [1])
    api.cast.current = cast_state(now_playing=np, profiles=[
        profile_state(1, used_s=100, watching=True), profile_state(2)])
    hub = api.app.state.admin_hub
    hub.publish(hub.reduce(api.cast.current))  # what the hub last saw
    api.cast.mode = "down"
    body = client.get(STATE, headers=reader).json()
    assert body["tv"]["connection"] == "unreachable" and body["tv"]["reachable"] is False
    assert body["now_playing"] is None
    assert [p["name"] for p in body["profiles"]] == ["Mila", "Noor"]


def test_cold_start_builds_profiles_from_the_db(client, api, reader):
    api.cast.mode = "down"
    body = client.get(STATE, headers=reader).json()
    assert body["tv"] == {"connection": "unreachable", "reachable": False, "device": None}
    assert body["now_playing"] is None and body["day"] == {"date": None, "resets_at": None}
    mila = body["profiles"][0]
    assert mila["name"] == "Mila" and mila["allowance_s"] == 3600 and mila["mode"] == "ignore_pauses"
    assert mila["used_s"] is None and mila["extra_s"] is None and mila["remaining_s"] is None
    assert mila["unlimited"] is False and mila["blocked"] is False and mila["watching"] is False


def test_jobs_and_disk_counts(client, api, reader):
    api.app.state.admin_counts.invalidate()
    api.conn.execute("INSERT INTO job (type, status, run_after, created_at, updated_at) VALUES ('download', 'queued', 'x', 'x', 'x')")
    for status in ("downloading", "processing", "failed"):
        api.conn.execute("INSERT INTO job (type, status, run_after, created_at, updated_at) VALUES ('download', ?, 'x', 'x', 'x')",
                         (status,))
    body = client.get(STATE, headers=reader).json()
    assert body["jobs"] == {"queued": 1, "running": 2, "failed": 1, "held_ready": 0}
    assert set(body["disk"]) == {"media_bytes", "free_bytes"}
    assert body["disk"]["media_bytes"] > 0  # the seeded thumbnails


def test_count_helpers(api):
    assert jobs.count_by_status(api.conn) == {}
    api.conn.execute("INSERT INTO job (type, status, run_after, created_at, updated_at) VALUES ('download', 'queued', 'x', 'x', 'x')")
    assert jobs.count_by_status(api.conn) == {"queued": 1}
    assert library.count_held_ready(api.conn) == 0


def test_held_ready_counts_only_ready_held_downloads(client, api, reader):
    for i, (status, publish) in enumerate([("ready", "hold"), ("ready", "hold"), ("downloading", "hold"),
                                            ("ready", "publish")]):
        api.conn.execute(
            "INSERT INTO source_video (youtube_id, url, title, status, publish, created_at, updated_at)"
            " VALUES (?, 'u', 't', ?, ?, 'x', 'x')", (f"yt{i}", status, publish))
    assert library.count_held_ready(api.conn) == 2
    api.app.state.admin_counts.invalidate()
    assert client.get(STATE, headers=reader).json()["jobs"]["held_ready"] == 2


def test_jobs_and_disk_are_cached_for_ten_seconds(client, api, reader, monkeypatch):
    import time
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    api.app.state.admin_counts.invalidate()
    assert client.get(STATE, headers=reader).json()["jobs"]["queued"] == 0
    api.conn.execute("INSERT INTO job (type, status, run_after, created_at, updated_at) VALUES ('download', 'queued', 'x', 'x', 'x')")
    now[0] += 9
    assert client.get(STATE, headers=reader).json()["jobs"]["queued"] == 0
    now[0] += 2
    assert client.get(STATE, headers=reader).json()["jobs"]["queued"] == 1
