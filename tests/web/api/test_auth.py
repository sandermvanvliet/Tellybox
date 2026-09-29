"""TokenGuard and /api/info (HA-1, HA-6)."""

from __future__ import annotations

import logging

import pytest

from tellybox import api_tokens
from tests.web.api.conftest import bearer
from tests.web.conftest import NOW

STATE = "/api/admin/state"


def test_info_needs_no_auth(client, api):
    r = client.get("/api/info")
    assert r.status_code == 200
    assert r.json() == {"instance_id": api_tokens.instance_id(api.conn), "version": api.config.version, "api": 1,
                        "capabilities": ["state", "events", "overrides", "profiles"]}


def test_missing_token_is_401_with_challenge(client):
    r = client.get(STATE)
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Basic abc", "tbx_whatever", "Bearer tbx_nope", "Bearer nope"])
def test_malformed_or_unknown_token_is_401(client, header):
    r = client.get(STATE, headers={"Authorization": header})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_revoked_token_is_401(client, api):
    token_id = api.conn.execute("SELECT id FROM api_token WHERE name = 'Tablet'").fetchone()[0]
    assert client.get(STATE, headers=bearer(api.read)).status_code == 200
    api_tokens.revoke_token(api.conn, token_id, NOW)
    assert client.get(STATE, headers=bearer(api.read)).status_code == 401


def test_bearer_scheme_is_case_insensitive(client, api):
    assert client.get(STATE, headers={"Authorization": f"bearer {api.read}"}).status_code == 200


def test_read_token_may_read_but_not_control(client, reader):
    assert client.get(STATE, headers=reader).status_code == 200
    for method, path, body in [
        ("POST", "/api/admin/overrides/extra", {"minutes": 5}),
        ("POST", "/api/admin/overrides/unlimited", {}),
        ("POST", "/api/admin/overrides/block", {}),
        ("POST", "/api/admin/overrides/stop", {}),
        ("DELETE", "/api/admin/overrides/today", None),
    ]:
        r = client.request(method, path, json=body, headers=reader)
        assert r.status_code == 403, path
        assert r.json() == {"detail": "forbidden"}


def test_control_token_implies_read(client, controller):
    assert client.get(STATE, headers=controller).status_code == 200


def test_every_admin_route_requires_a_token(client):
    for method, path in [("GET", STATE), ("GET", "/api/admin/events"), ("POST", "/api/admin/overrides/extra"),
                         ("POST", "/api/admin/overrides/unlimited"), ("POST", "/api/admin/overrides/block"),
                         ("POST", "/api/admin/overrides/stop"), ("DELETE", "/api/admin/overrides/today")]:
        assert client.request(method, path).status_code == 401, path


def test_auth_records_last_use_and_sets_no_cookie(client, api):
    r = client.get(STATE, headers=bearer(api.read))
    assert "set-cookie" not in r.headers
    row = api.conn.execute("SELECT last_used_at FROM api_token WHERE name = 'Tablet'").fetchone()
    assert row[0] is not None


def test_the_token_is_never_logged(client, api, caplog):
    with caplog.at_level(logging.DEBUG):
        client.get(STATE, headers=bearer(api.read))
        client.get(STATE, headers=bearer(api.read + "x"))
    assert api.read not in caplog.text
