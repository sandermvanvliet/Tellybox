"""Admin sign-in suite (AD-1, NF-2): the contract in tellybox/web/admin/{__init__,auth,common}.py.

Bugs found in those contract files are not fixed here (see the report); a test that
exposes one is kept and may stay red until they're addressed.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tellybox.config import admin_password_from_env
from tellybox import auth
from tellybox.web.app import create_app
from tests.web.admin.conftest import ORIGIN, PASSWORD, sign_in

# --------------------------------------------------------------------------- login / logout


def test_login_then_logout_ends_the_session(admin_env):
    client = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    sign_in(client)
    assert client.get("/admin/jobs").status_code == 200

    old_cookie = client.cookies.get(auth.SESSION_COOKIE)
    r = client.post("/admin/logout", follow_redirects=False)
    assert r.status_code == 303

    assert client.get("/admin/jobs", follow_redirects=False).status_code in (401, 303)

    # The old token, even reintroduced by hand, is dead too.
    client.cookies.set(auth.SESSION_COOKIE, old_cookie)
    assert client.get("/admin/jobs", follow_redirects=False).status_code in (401, 303)


def test_wrong_password_is_401_with_a_message(anon):
    r = anon.post("/admin/login", data={"password": "not it", "next": "/admin"})
    assert r.status_code == 401
    assert "wrong password" in r.text.lower()


# --------------------------------------------------------------------------- session cookie


def test_session_cookie_flags(anon):
    r = sign_in(anon)
    set_cookie = r.headers["set-cookie"]
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    assert "path=/admin" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()  # no X-Forwarded-Proto: https on this request


def test_session_cookie_is_secure_behind_an_https_proxy(admin_env):
    client = TestClient(admin_env.app, headers={"Origin": ORIGIN, "X-Forwarded-Proto": "https"})
    r = sign_in(client)
    assert "secure" in r.headers["set-cookie"].lower()


# --------------------------------------------------------------------------- 30-day expiry


def test_session_slides_but_expires_after_30_idle_days(admin_env):
    client = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    sign_in(client)
    clock = admin_env.clock

    def visit():
        return client.get("/admin/jobs", headers={"Accept": "text/html"}, follow_redirects=False)

    clock.advance(timedelta(days=29, hours=23).total_seconds())
    assert visit().status_code == 200  # just under 30 days: still valid, and the visit restarts the clock

    clock.advance(timedelta(days=29, hours=23).total_seconds())
    assert visit().status_code == 200  # sliding expiry: the second visit is also within 30 days of the first

    clock.advance(timedelta(days=30, hours=1).total_seconds())
    r3 = visit()
    assert r3.status_code == 303 and "/admin/login" in r3.headers["location"]


# --------------------------------------------------------------------------- password change


def test_changing_the_password_ends_all_sessions(admin_env):
    client = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    sign_in(client)
    assert client.get("/admin/jobs").status_code == 200

    auth.install_password(admin_env.conn, "a new password")

    r = client.get("/admin/jobs", follow_redirects=False)
    assert r.status_code in (401, 303)


# --------------------------------------------------------------------------- locked state


def test_locked_state_blocks_admin_login_and_pages(config, lib, fake_cast, clock, ytdlp, admin_static):
    conn, _ = lib
    app = create_app(config, conn=conn, cast=fake_cast, clock=clock, static_dir=admin_static, ytdlp=ytdlp)
    client = TestClient(app, headers={"Origin": ORIGIN})
    for path in ("/admin", "/admin/login", "/admin/jobs"):
        r = client.get(path)
        assert r.status_code == 503, f"{path} -> {r.status_code}"
        assert "Admin is locked: set TELLYBOX_ADMIN_PASSWORD" in r.text


# --------------------------------------------------------------------------- Origin check


def test_login_post_without_origin_or_referer_is_refused(admin_env):
    client = TestClient(admin_env.app)  # no Origin/Referer at all
    r = client.post("/admin/login", data={"password": PASSWORD, "next": "/admin"})
    assert r.status_code == 403


def test_login_post_with_a_foreign_origin_is_refused(admin_env):
    client = TestClient(admin_env.app, headers={"Origin": "https://evil.example"})
    r = client.post("/admin/login", data={"password": PASSWORD, "next": "/admin"})
    assert r.status_code == 403


def test_guarded_post_without_origin_is_refused(admin_env):
    signed_in = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    sign_in(signed_in)
    bare = TestClient(admin_env.app, cookies=signed_in.cookies)
    r = bare.post("/admin/jobs/update-ytdlp")
    assert r.status_code == 403


def test_guarded_post_with_a_foreign_origin_is_refused(admin_env):
    signed_in = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    sign_in(signed_in)
    foreign = TestClient(admin_env.app, cookies=signed_in.cookies, headers={"Origin": "https://evil.example"})
    r = foreign.post("/admin/jobs/update-ytdlp")
    assert r.status_code == 403


# --------------------------------------------------------------------------- login throttling


def _wait_seconds(r) -> int:
    m = re.search(r"(\d+) s", r.text)
    assert m, r.text
    return int(m.group(1))


def test_login_throttle_doubles_then_caps_and_clears_on_success(admin_env):
    client = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    clock = admin_env.clock

    def wrong():
        return client.post("/admin/login", data={"password": "nope", "next": "/admin"})

    waits = []
    for _ in range(8):
        r = wrong()
        assert r.status_code == 401, r.text  # the previous wait had fully elapsed
        blocked = wrong()  # immediate retry: still inside the new wait
        assert blocked.status_code == 429, blocked.text
        wait = _wait_seconds(blocked)
        waits.append(wait)
        clock.advance(wait)

    assert waits[:6] == [1, 2, 4, 8, 16, 32]
    assert waits[6] == 60 and waits[7] == 60  # capped

    ok = client.post("/admin/login", data={"password": PASSWORD, "next": "/admin"}, follow_redirects=False)
    assert ok.status_code == 303

    after = wrong()
    assert after.status_code == 401  # not 429: the throttle was cleared by the successful login


# --------------------------------------------------------------------------- next redirect safety


@pytest.mark.parametrize("unsafe", ["//evil.com", "https://evil.com"])
def test_next_redirect_falls_back_to_admin(admin_env, unsafe):
    client = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    r = client.post("/admin/login", data={"password": PASSWORD, "next": unsafe}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


# --------------------------------------------------------------------------- every route requires sign-in


def _fill_path_params(path: str) -> str:
    return re.sub(r"\{[^}/]+\}", "1", path)


def _iter_endpoints(routes):
    """Flatten FastAPI's included-router tree down to individual path operations.

    Newer FastAPI (0.141) keeps `app.routes` as a tree of `_IncludedRouter` wrapper
    nodes instead of a flat list of APIRoute objects; descend into
    `original_router.routes` (or plain `.routes`, for a Mount) to reach the leaves.
    """
    for route in routes:
        sub = getattr(route, "routes", None)
        if sub is None:
            original = getattr(route, "original_router", None)
            sub = getattr(original, "routes", None) if original is not None else None
        if sub is not None:
            yield from _iter_endpoints(sub)
        else:
            yield route


def test_every_admin_route_refuses_anyone_not_signed_in(admin_env):
    app = admin_env.app
    client = TestClient(app, headers={"Origin": ORIGIN})  # same-origin, but never signed in
    checked = 0
    for route in _iter_endpoints(app.routes):
        path = getattr(route, "path", None)
        if not path or not path.startswith("/admin"):
            continue
        if path == "/admin/login" or path.startswith("/admin/static"):
            continue
        methods = (getattr(route, "methods", None) or set()) - {"HEAD"}
        target = _fill_path_params(path)
        for method in methods:
            checked += 1
            r = client.request(method, target, follow_redirects=False)
            ok = r.status_code == 401 or (r.status_code == 303 and "/admin/login" in r.headers.get("location", ""))
            assert ok, f"{method} {target} -> {r.status_code} (expected 401 or a redirect to login)"
    assert checked > 0  # the loop actually found admin routes to check


# --------------------------------------------------------------------------- config


def test_admin_password_from_env_prefers_direct_value():
    assert admin_password_from_env({"TELLYBOX_ADMIN_PASSWORD": "direct"}) == "direct"


def test_admin_password_from_env_reads_file_and_strips_trailing_newline(tmp_path):
    path = tmp_path / "pw.txt"
    path.write_text("s3cret\n")
    assert admin_password_from_env({"TELLYBOX_ADMIN_PASSWORD_FILE": str(path)}) == "s3cret"


def test_admin_password_from_env_is_none_when_unset():
    assert admin_password_from_env({}) is None
    assert admin_password_from_env({"TELLYBOX_ADMIN_PASSWORD": ""}) is None


# --------------------------------------------------------------------------- behind nginx (step 6)


def _throttled_addresses(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT address FROM login_throttle ORDER BY address")]


def test_login_throttle_uses_the_forwarded_address_from_nginx(admin_env):
    """nginx on the same host is the only trusted proxy; each device gets its own throttle."""
    via_nginx = TestClient(admin_env.app, client=("127.0.0.1", 50000), headers={"Origin": "https://testserver"})
    r = via_nginx.post("/admin/login", data={"password": "wrong"},
                       headers={"X-Forwarded-For": "192.168.1.50", "X-Forwarded-Proto": "https"})
    assert r.status_code == 401
    assert _throttled_addresses(admin_env.conn) == ["192.168.1.50"]


def test_forwarded_headers_from_other_clients_are_ignored(admin_env):
    direct = TestClient(admin_env.app, client=("192.168.1.60", 50000), headers={"Origin": "http://testserver"})
    direct.post("/admin/login", data={"password": "wrong"}, headers={"X-Forwarded-For": "10.9.9.9"})
    assert _throttled_addresses(admin_env.conn) == ["192.168.1.60"]


def test_session_cookie_is_secure_through_nginx_https(admin_env):
    via_nginx = TestClient(admin_env.app, client=("127.0.0.1", 50000), headers={"Origin": "https://testserver"})
    r = via_nginx.post("/admin/login", data={"password": PASSWORD, "next": "/admin"},
                       headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    assert r.status_code == 303
    assert "secure" in r.headers["set-cookie"].lower()


# --------------------------------------------------------------------------- branding


def test_login_page_links_the_favicon(anon):
    html = anon.get("/admin/login").text
    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in html
    assert '<link rel="icon" href="/favicon.ico" sizes="32x32">' in html


def test_signed_in_header_shows_the_mark(admin):
    html = admin.get("/admin").text
    assert re.search(r'<a class="brand" href="/admin"><img src="/static/mark-dark.svg" alt="" [^>]*>Tellybox</a>', html)
