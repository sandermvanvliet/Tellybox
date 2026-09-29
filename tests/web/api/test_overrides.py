"""Override endpoints (HA-4, HA-5, HA-7, HA-8) and the shared apply_override."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tellybox.web.cast_client import CastUnavailable
from tellybox.web.overrides import apply_override
from tests.web.conftest import FakeCast, cast_state

BASE = "/api/admin/overrides"


def last_call(api):
    return api.cast.calls[-1]


def test_extra_minutes(client, api, controller):
    r = client.post(f"{BASE}/extra", json={"minutes": 15, "profile_ids": [1]}, headers=controller)
    assert r.status_code == 200
    assert last_call(api) == ("override", "extra_minutes", 15, [1], "Home Assistant")
    assert r.json()["api"] == 1 and r.json()["profiles"][0]["name"] == "Mila"  # an AdminState


def test_extra_minutes_for_everyone(client, api, controller):
    client.post(f"{BASE}/extra", json={"minutes": 10}, headers=controller)
    assert last_call(api) == ("override", "extra_minutes", 10, None, "Home Assistant")


@pytest.mark.parametrize("route, kind", [("unlimited", "unlimited"), ("block", "block")])
def test_unlimited_and_block(client, api, controller, route, kind):
    r = client.post(f"{BASE}/{route}", json={"profile_ids": [1, 2]}, headers=controller)
    assert r.status_code == 200
    assert last_call(api) == ("override", kind, None, [1, 2], "Home Assistant")


@pytest.mark.parametrize("route", ["unlimited", "block", "stop"])
def test_body_is_optional(client, api, controller, route):
    assert client.post(f"{BASE}/{route}", headers=controller).status_code == 200


def test_stop_now(client, api, controller):
    assert client.post(f"{BASE}/stop", json={}, headers=controller).status_code == 200
    assert last_call(api) == ("override", "stop_now", None, None, "Home Assistant")


def test_clear_today(client, api, controller):
    r = client.delete(f"{BASE}/today", headers=controller)
    assert r.status_code == 200
    assert last_call(api) == ("override", "clear", None, None, "Home Assistant")


def test_clear_today_with_profile_ids_query(client, api, controller):
    client.delete(f"{BASE}/today?profile_ids=1,2", headers=controller)
    assert last_call(api) == ("override", "clear", None, [1, 2], "Home Assistant")


def test_source_is_the_token_name(client, api):
    from tellybox import api_tokens
    from tests.web.api.conftest import bearer
    from tests.web.conftest import NOW
    _, secret = api_tokens.create_token(conn=api.conn, name="Hallway", scopes=["control"], now=NOW)
    client.post(f"{BASE}/stop", headers=bearer(secret))
    assert last_call(api)[-1] == "Hallway"


def test_response_is_built_from_the_returned_cast_state(client, api, controller):
    api.cast.current = cast_state(remaining_s=4242, profiles=[
        {"profile_id": 1, "day": "2026-09-28", "used_s": 1, "extra_s": 900, "unlimited": False, "blocked": True,
         "remaining_s": 4242, "can_start": False, "reason": "blocked", "session_elapsed_s": None, "watching": False}])
    body = client.post(f"{BASE}/block", json={"profile_ids": [1]}, headers=controller).json()
    assert body["group"]["remaining_s"] == 4242
    assert body["profiles"][0]["blocked"] is True and body["profiles"][0]["extra_s"] == 900


@pytest.mark.parametrize("minutes", [0, 241, -5, "ten", None, 1.5])
def test_extra_minutes_out_of_range_is_422(client, api, controller, minutes):
    r = client.post(f"{BASE}/extra", json={"minutes": minutes}, headers=controller)
    assert r.status_code == 422
    assert "detail" in r.json()
    assert not any(c[0] == "override" for c in api.cast.calls)


@pytest.mark.parametrize("edge", [1, 240])
def test_extra_minutes_edges_are_accepted(client, api, controller, edge):
    assert client.post(f"{BASE}/extra", json={"minutes": edge}, headers=controller).status_code == 200


@pytest.mark.parametrize("ids", [[], [1, 1], list(range(1, 22)), ["a"], [0], "1"])
def test_bad_profile_ids_are_422(client, api, controller, ids):
    r = client.post(f"{BASE}/block", json={"profile_ids": ids}, headers=controller)
    assert r.status_code == 422
    assert not any(c[0] == "override" for c in api.cast.calls)


def test_bad_query_profile_ids_are_422(client, api, controller):
    for q in ("", "1,1", "a", "1,,2"):
        assert client.delete(f"{BASE}/today?profile_ids={q}", headers=controller).status_code == 422, q


def test_unknown_profile_is_422_from_the_cast_service(client, api, controller):
    api.cast.mode = "invalid"
    r = client.post(f"{BASE}/block", json={"profile_ids": [99]}, headers=controller)
    assert r.status_code == 422 and "detail" in r.json()


def test_unknown_field_in_body_is_422(client, controller):
    assert client.post(f"{BASE}/extra", json={"minutes": 5, "profiles": [1]}, headers=controller).status_code == 422


@pytest.mark.parametrize("method, route, body", [
    ("POST", "extra", {"minutes": 5}), ("POST", "unlimited", {}), ("POST", "block", {}), ("POST", "stop", {}),
    ("DELETE", "today", None)])
def test_cast_service_down_is_503(client, api, controller, method, route, body):
    api.cast.mode = "down"
    r = client.request(method, f"{BASE}/{route}", json=body, headers=controller)
    assert r.status_code == 503
    assert r.json() == {"detail": "cast_unavailable"}


# ------------------------------------------------------------------ one code path for HTML and JSON


def test_dashboard_and_json_make_identical_cast_calls(api, controller):
    import dataclasses
    from fastapi.testclient import TestClient
    from tellybox.web.app import create_app
    from tests.web.admin.conftest import ORIGIN, PASSWORD, sign_in

    cast = FakeCast()
    app = create_app(dataclasses.replace(api.config, admin_password=PASSWORD), conn=api.conn, cast=cast,
                     clock=api.clock, ytdlp=SimpleNamespace())
    html = TestClient(app, headers={"Origin": ORIGIN})
    assert sign_in(html).status_code == 303
    html.post("/admin/overrides", data={"kind": "extra_minutes", "value": "15", "profile_id": "1"})
    html.post("/admin/overrides", data={"kind": "block", "profile_id": "1"})
    html.post("/admin/overrides", data={"kind": "stop_now"})
    json_client = TestClient(app)
    json_client.post(f"{BASE}/extra", json={"minutes": 15, "profile_ids": [1]}, headers=controller)
    json_client.post(f"{BASE}/block", json={"profile_ids": [1]}, headers=controller)
    json_client.post(f"{BASE}/stop", headers=controller)
    calls = [c for c in cast.calls if c[0] == "override"]
    assert [c[:4] for c in calls[:3]] == [c[:4] for c in calls[3:]]
    assert [c[4] for c in calls[:3]] == [None] * 3 and [c[4] for c in calls[3:]] == ["Home Assistant"] * 3


# ------------------------------------------------------------------ apply_override


async def test_apply_override_returns_the_cast_state():
    cast = FakeCast()
    assert await apply_override(cast, "extra_minutes", 15, [1], "HA") is cast.current
    assert cast.calls == [("override", "extra_minutes", 15, [1], "HA")]


@pytest.mark.parametrize("kind, value, ids", [
    ("nope", None, None), ("extra_minutes", None, None), ("extra_minutes", 0, None), ("extra_minutes", 241, None),
    ("block", None, []), ("block", None, [1, 1]), ("block", None, list(range(1, 22)))])
async def test_apply_override_validates_before_calling_cast(kind, value, ids):
    cast = FakeCast()
    with pytest.raises(ValueError):
        await apply_override(cast, kind, value, ids)
    assert cast.calls == []


async def test_apply_override_passes_value_through_for_toggles():
    cast = FakeCast()
    await apply_override(cast, "unlimited", 0, [1])
    assert cast.calls == [("override", "unlimited", 0, [1], None)]


async def test_apply_override_lets_cast_unavailable_through():
    cast = FakeCast()
    cast.mode = "down"
    with pytest.raises(CastUnavailable):
        await apply_override(cast, "stop_now")
