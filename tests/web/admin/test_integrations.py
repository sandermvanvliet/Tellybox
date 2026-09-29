"""Integrations page `/admin/integrations` (HA-1): create, list and revoke API tokens."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tellybox import api_tokens

SECRET = re.compile(r"tbx_[A-Za-z0-9_-]{43}")


def _create(admin, name="Home Assistant", scope="control"):
    return admin.post("/admin/integrations", data={"name": name, "scope": scope}, follow_redirects=False)


def test_page_renders_with_help_and_nav(admin):
    r = admin.get("/admin/integrations")
    assert r.status_code == 200
    assert "Integrations" in r.text
    assert "docs/admin-api.md" in r.text
    assert "Cast" in r.text  # the HA Cast entity warning (HA-8)


def test_requires_sign_in(anon):
    r = anon.get("/admin/integrations", follow_redirects=False)
    assert r.status_code in (401, 303)
    r = anon.post("/admin/integrations", data={"name": "x", "scope": "read"}, follow_redirects=False)
    assert r.status_code in (401, 303)
    assert not SECRET.search(r.text)


def test_create_shows_secret_once_and_stores_only_hash(admin, admin_env):
    r = _create(admin)
    assert r.status_code == 200  # rendered directly, not redirected
    found = SECRET.findall(r.text)
    assert found and len(set(found)) == 1
    secret = found[0]
    assert "it won't be shown again" in r.text.replace("&#39;", "'")
    assert "location" not in r.headers
    assert r.headers["cache-control"] == "no-store"

    rows = admin_env.conn.execute("SELECT * FROM api_token").fetchall()
    assert len(rows) == 1
    assert secret not in "".join(str(tuple(row)) for row in rows)
    token = api_tokens.authenticate(admin_env.conn, secret, admin_env.clock.now())
    assert token is not None and token.name == "Home Assistant"
    assert token.allows(api_tokens.CONTROL)

    again = admin.get("/admin/integrations")
    assert secret not in again.text
    assert "Home Assistant" in again.text


def test_read_only_scope(admin, admin_env):
    r = _create(admin, "Hallway tablet", "read")
    secret = SECRET.search(r.text).group(0)
    token = api_tokens.authenticate(admin_env.conn, secret, admin_env.clock.now())
    assert token.allows(api_tokens.READ) and not token.allows(api_tokens.CONTROL)
    assert "Read only" in admin.get("/admin/integrations").text


def test_bad_scope_is_refused(admin, admin_env):
    r = _create(admin, "x", "root")
    assert r.status_code == 422
    assert admin_env.conn.execute("SELECT COUNT(*) FROM api_token").fetchone()[0] == 0


def test_empty_or_long_name_is_refused(admin, admin_env):
    for name in ("", "   ", "x" * 41):
        r = _create(admin, name)
        assert r.status_code == 422, name
        assert not SECRET.search(r.text)
    assert admin_env.conn.execute("SELECT COUNT(*) FROM api_token").fetchone()[0] == 0


def test_name_is_escaped(admin):
    r = _create(admin, "<i>x</i>")
    assert "<i>x</i>" not in r.text
    assert "<i>x</i>" not in admin.get("/admin/integrations").text


def test_list_shows_last_used_never_and_revoked(admin, admin_env):
    api_tokens.create_token(admin_env.conn, "Live one", ["control"], admin_env.clock.now())
    rid, _ = api_tokens.create_token(admin_env.conn, "Old one", ["read"], admin_env.clock.now())
    api_tokens.revoke_token(admin_env.conn, rid, admin_env.clock.now())
    text = admin.get("/admin/integrations").text
    assert "Live one" in text and "Old one" in text
    assert "never" in text
    assert "revoked" in text.lower()


def test_revoke(admin, admin_env):
    tid, secret = api_tokens.create_token(admin_env.conn, "HA", ["control"], admin_env.clock.now())
    r = admin.post(f"/admin/integrations/{tid}/revoke", follow_redirects=False)
    assert r.status_code == 303
    assert api_tokens.authenticate(admin_env.conn, secret, admin_env.clock.now()) is None
    assert api_tokens.get_token(admin_env.conn, tid).revoked


def test_revoke_unknown_is_404(admin):
    assert admin.post("/admin/integrations/999/revoke").status_code == 404


def test_post_without_origin_is_refused(admin_env, admin):
    tid, secret = api_tokens.create_token(admin_env.conn, "HA", ["control"], admin_env.clock.now())
    client = TestClient(admin_env.app)
    client.cookies.update(admin.cookies)
    assert client.post("/admin/integrations", data={"name": "x", "scope": "read"}).status_code == 403
    assert client.post(f"/admin/integrations/{tid}/revoke").status_code == 403
    assert api_tokens.authenticate(admin_env.conn, secret, admin_env.clock.now()) is not None
    assert admin_env.conn.execute("SELECT COUNT(*) FROM api_token").fetchone()[0] == 1
