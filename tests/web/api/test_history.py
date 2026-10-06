"""GET /api/admin/history (HA-12, A-38): daily totals and the last watched episode, from the database."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from tellybox import store
from tellybox.cast.controller import EndReason
from tests.web.api.conftest import bearer
from tests.web.conftest import NOW

URL = "/api/admin/history"
ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00$")


def usage(api, profile, day, seconds, extra_min=0, unlimited=0, blocked=0):
    api.conn.execute(
        "INSERT INTO daily_usage (profile_id, day, seconds_used, extra_min, unlimited, blocked) VALUES (?, ?, ?, ?, ?, ?)",
        (profile, day, seconds, extra_min, unlimited, blocked))


def get(client, reader, query=""):
    return client.get(URL + query, headers=reader)


def today(client, reader) -> str:
    return get(client, reader, "?days=1").json()["today"]


def test_shape_and_values_from_seeded_rows(client, api, reader):  # HA-12
    t = today(client, reader)
    y = (api.clock.now().astimezone(api.config.tz).date() - timedelta(days=1)).isoformat()
    usage(api, 1, t, 1200.4)
    usage(api, 1, y, 2710, extra_min=15, unlimited=1)
    usage(api, 2, t, 60, blocked=1)
    body = get(client, reader, "?days=2").json()
    assert body["today"] == t and body["days"] == 2
    assert [p["id"] for p in body["profiles"]] == [1, 2]
    mila, noor = body["profiles"]
    assert mila["name"] == "Mila" and noor["name"] == "Noor"
    assert mila["days"][0] == {"date": t, "used_s": 1200, "extra_s": 0, "unlimited": False, "blocked": False}
    assert mila["days"][1] == {"date": y, "used_s": 2710, "extra_s": 900, "unlimited": True, "blocked": False}
    assert noor["days"][0]["blocked"] is True and noor["days"][1] == {
        "date": y, "used_s": 0, "extra_s": 0, "unlimited": False, "blocked": False}
    assert set(mila) == {"id", "name", "days", "last_watched"}
    assert mila["last_watched"] is None


def test_default_is_seven_days_newest_first(client, reader):
    body = get(client, reader).json()
    assert body["days"] == 7
    dates = [d["date"] for d in body["profiles"][0]["days"]]
    assert len(dates) == 7 and dates == sorted(dates, reverse=True) and dates[0] == body["today"]


@pytest.mark.parametrize("days", [1, 21])
def test_days_bounds_are_accepted(client, reader, days):
    body = get(client, reader, f"?days={days}").json()
    assert body["days"] == days and all(len(p["days"]) == days for p in body["profiles"])


@pytest.mark.parametrize("days", ["0", "22", "x", "", "1.5", "-1"])
def test_bad_days_is_422(client, reader, days):
    r = get(client, reader, f"?days={days}")
    assert r.status_code == 422
    assert list(r.json()) == ["detail"] and isinstance(r.json()["detail"], str)


def test_the_422_body_for_days_out_of_range(client, reader):
    assert get(client, reader, "?days=22").json() == {"detail": "days must be between 1 and 21"}
    assert get(client, reader, "?days=x").json() == {"detail": "days must be an integer"}


def test_profile_ids_filter_and_keep_the_admins_order(client, reader):
    only = get(client, reader, "?profile_ids=2").json()
    assert [p["id"] for p in only["profiles"]] == [2]
    both = get(client, reader, "?profile_ids=2,1").json()
    assert [p["id"] for p in both["profiles"]] == [1, 2]  # sort_order, not request order


@pytest.mark.parametrize("ids", ["99", "1,99", "a,b", "1,,2", "1,x"])
def test_unknown_or_bad_profile_ids_is_422(client, reader, ids):
    r = get(client, reader, f"?profile_ids={ids}")
    assert r.status_code == 422 and list(r.json()) == ["detail"]


def test_empty_profile_ids_means_everyone(client, reader):
    assert [p["id"] for p in get(client, reader, "?profile_ids=").json()["profiles"]] == [1, 2]


def test_last_watched_open_and_closed_with_iso_utc_timestamps(client, api, reader):
    started = NOW - timedelta(minutes=30)
    closed = store.open_watch_session(api.conn, api.ids.b1, [1], started - timedelta(hours=2))
    store.close_watch_session(api.conn, closed, EndReason.FINISHED, started - timedelta(hours=1), 600.0)
    store.open_watch_session(api.conn, api.ids.a1, [2], started, target="device")
    profiles = {p["id"]: p for p in get(client, reader).json()["profiles"]}
    mila, noor = profiles[1]["last_watched"], profiles[2]["last_watched"]
    assert mila["episode_id"] == api.ids.b1 and mila["title"] == "Title b1" and mila["show"] == "Bravo"
    assert mila["target"] == "tv" and ISO_UTC.match(mila["started_at"]) and ISO_UTC.match(mila["ended_at"])
    assert noor["episode_id"] == api.ids.a1 and noor["target"] == "device"
    assert ISO_UTC.match(noor["started_at"]) and noor["ended_at"] is None  # watching now


def test_a_deleted_episode_gives_null_ids_and_titles(client, api, reader):
    sid = store.open_watch_session(api.conn, api.ids.b1, [1], NOW)
    store.close_watch_session(api.conn, sid, EndReason.STOPPED, NOW + timedelta(minutes=1), 60.0)
    api.conn.execute("PRAGMA foreign_keys = OFF")
    api.conn.execute("UPDATE watch_session SET episode_id = NULL WHERE id = ?", (sid,))
    lw = get(client, reader).json()["profiles"][0]["last_watched"]
    assert lw["episode_id"] is None and lw["title"] is None and lw["show"] is None


def test_401_without_or_with_a_bad_token(client):
    r = client.get(URL)
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}
    assert client.get(URL, headers=bearer("tbx_nope")).status_code == 401


def test_a_read_token_works(client, reader):
    assert get(client, reader).status_code == 200


def test_answers_while_the_cast_service_is_down(client, api, reader):
    api.cast.mode = "down"
    r = get(client, reader)
    assert r.status_code == 200 and len(r.json()["profiles"]) == 2


def test_the_capability_is_listed(client):
    assert "history" in client.get("/api/info").json()["capabilities"]


def test_the_token_is_not_in_the_response(client, api, reader):
    assert api.read not in get(client, reader).text
