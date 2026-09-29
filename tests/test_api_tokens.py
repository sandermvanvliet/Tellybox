"""API tokens (HA-1, HA-6): tellybox/api_tokens.py."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tellybox import api_tokens, db

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    return db.open_db(":memory:")


def test_create_returns_the_secret_once_and_stores_only_its_hash(conn):
    token_id, secret = api_tokens.create_token(conn, "Home Assistant", ["read", "control"], NOW)
    assert secret.startswith("tbx_") and len(secret) == 4 + 43
    row = conn.execute("SELECT * FROM api_token WHERE id = ?", (token_id,)).fetchone()
    assert row["token_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in [str(v) for v in tuple(row)]
    assert row["scopes"] == "read control"


def test_authenticate_accepts_the_secret_and_refuses_others(conn):
    _, secret = api_tokens.create_token(conn, "HA", ["control"], NOW)
    token = api_tokens.authenticate(conn, secret, NOW)
    assert token is not None and token.name == "HA"
    assert api_tokens.authenticate(conn, secret + "x", NOW) is None
    assert api_tokens.authenticate(conn, "", NOW) is None
    assert api_tokens.authenticate(conn, None, NOW) is None
    assert api_tokens.authenticate(conn, "Bearer " + secret, NOW) is None


def test_revoked_tokens_are_refused_but_stay_listed(conn):
    token_id, secret = api_tokens.create_token(conn, "HA", ["read"], NOW)
    assert api_tokens.revoke_token(conn, token_id, NOW)
    assert not api_tokens.revoke_token(conn, token_id, NOW + timedelta(hours=1))  # keeps the first time
    assert api_tokens.authenticate(conn, secret, NOW) is None
    [listed] = api_tokens.list_tokens(conn)
    assert listed.revoked and listed.revoked_at == NOW


def test_control_implies_read(conn):
    _, ro = api_tokens.create_token(conn, "Tablet", ["read"], NOW)
    _, rw = api_tokens.create_token(conn, "HA", ["control"], NOW)
    ro_token = api_tokens.authenticate(conn, ro, NOW)
    rw_token = api_tokens.authenticate(conn, rw, NOW)
    assert ro_token.allows("read") and not ro_token.allows("control")
    assert rw_token.allows("read") and rw_token.allows("control")


@pytest.mark.parametrize("name, scopes", [("", ["read"]), ("x" * 41, ["read"]), ("HA", []), ("HA", ["admin"])])
def test_bad_names_and_scopes_are_refused(conn, name, scopes):
    with pytest.raises(ValueError):
        api_tokens.create_token(conn, name, scopes, NOW)


def test_last_used_is_written_at_most_once_a_minute(conn):
    token_id, secret = api_tokens.create_token(conn, "HA", ["read"], NOW)
    api_tokens.authenticate(conn, secret, NOW)
    api_tokens.authenticate(conn, secret, NOW + timedelta(seconds=30))
    assert api_tokens.get_token(conn, token_id).last_used_at == NOW
    api_tokens.authenticate(conn, secret, NOW + timedelta(seconds=61))
    assert api_tokens.get_token(conn, token_id).last_used_at == NOW + timedelta(seconds=61)


def test_list_puts_live_tokens_first(conn):
    old_id, _ = api_tokens.create_token(conn, "Old", ["read"], NOW)
    api_tokens.create_token(conn, "New", ["read"], NOW + timedelta(minutes=1))
    api_tokens.revoke_token(conn, old_id, NOW)
    assert [t.name for t in api_tokens.list_tokens(conn)] == ["New", "Old"]


def test_instance_id_is_set_and_stable(conn):
    first = api_tokens.instance_id(conn)
    assert len(first) == 32 and int(first, 16) >= 0
    assert api_tokens.instance_id(conn) == first
