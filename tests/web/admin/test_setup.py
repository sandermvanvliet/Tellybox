"""First-run admin password with a setup code (DP-4): tellybox/auth.py and /admin/setup."""

from __future__ import annotations

import logging
import re

import pytest
from fastapi.testclient import TestClient

from tellybox import auth
from tellybox.web.app import create_app
from tests.web.admin.conftest import ORIGIN, PASSWORD, sign_in


@pytest.fixture
def fresh(config, lib, fake_cast, clock, ytdlp, admin_static, caplog):
    """An app without any password: the setup page is open and a code was logged."""
    conn, _ = lib
    with caplog.at_level(logging.WARNING):
        app = create_app(config, conn=conn, cast=fake_cast, clock=clock, static_dir=admin_static, ytdlp=ytdlp)
    code = re.search(r"admin setup code: ([A-Z0-9]{4}-[A-Z0-9]{4}) ", caplog.text).group(1)
    client = TestClient(app, headers={"Origin": ORIGIN})
    return client, conn, code, app


def _setup(client, code, password="a long enough one", repeat=None):
    return client.post("/admin/setup", data={"code": code, "password": password,
                                             "password2": password if repeat is None else repeat},
                       follow_redirects=False)


# --------------------------------------------------------------------------- precedence


def test_env_password_is_stored_with_source_env(admin_env):
    row = admin_env.conn.execute("SELECT admin_password_source, admin_setup_code_hash FROM settings").fetchone()
    assert tuple(row) == ("env", None)


def test_ui_password_is_kept_when_no_env_password(fresh):
    client, conn, code, _ = fresh
    assert _setup(client, code).status_code == 303
    auth.install_password(conn, None)  # next start, still no env password
    assert not auth.is_locked(conn) and auth.check_password(conn, "a long enough one")
    assert client.get("/admin/jobs").status_code == 200  # session survived


def test_env_password_wins_over_ui_and_ends_sessions(fresh):
    client, conn, code, _ = fresh
    _setup(client, code)
    auth.install_password(conn, "from the environment")
    assert conn.execute("SELECT admin_password_source FROM settings").fetchone()[0] == "env"
    assert auth.check_password(conn, "from the environment") and not auth.check_password(conn, "a long enough one")
    assert client.get("/admin/jobs", follow_redirects=False).status_code in (303, 401)


def test_removed_env_password_locks_admin_again(admin_env):
    auth.install_password(admin_env.conn, None)
    assert auth.is_locked(admin_env.conn)
    assert admin_env.conn.execute("SELECT count(*) FROM admin_session").fetchone()[0] == 0


# --------------------------------------------------------------------------- the setup page


def test_code_is_logged_and_only_its_hash_stored(fresh):
    _, conn, code, _ = fresh
    stored = conn.execute("SELECT admin_setup_code_hash, admin_setup_code_created_at FROM settings").fetchone()
    assert stored[0] and code.replace("-", "") not in stored[0] and stored[1]
    assert not set(code.replace("-", "")) & set("01ILO")


def test_every_start_makes_a_new_code(config, lib, fake_cast, clock, ytdlp, admin_static, caplog):
    conn, _ = lib
    codes = []
    for _i in range(2):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            create_app(config, conn=conn, cast=fake_cast, clock=clock, static_dir=admin_static, ytdlp=ytdlp)
        codes.append(re.search(r"setup code: (\S+)", caplog.text).group(1))
    assert codes[0] != codes[1]
    assert not auth.check_setup_code(conn, codes[0]) and auth.check_setup_code(conn, codes[1])


def test_no_code_is_logged_when_a_password_exists(admin_env, caplog):
    assert "setup code" not in caplog.text


def test_locked_pages_send_browsers_to_setup(fresh):
    client, *_ = fresh
    for path in ("/admin", "/admin/login", "/admin/jobs"):
        r = client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/setup", path
    page = client.get("/admin/setup")
    assert page.status_code == 200 and "docker compose logs web" in page.text


def test_locked_non_browser_requests_get_401(fresh):
    client, *_ = fresh
    r = client.get("/admin/jobs", headers={"Accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401


def test_setup_is_closed_once_a_password_exists(admin_env):
    client = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    r = client.get("/admin/setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    r = _setup(client, "ABCD-EFGH")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert auth.check_password(admin_env.conn, PASSWORD)  # untouched


def test_wrong_code_is_refused_and_throttled(fresh):
    client, conn, code, _ = fresh
    r = _setup(client, "AAAA-AAAA")
    assert r.status_code == 401 and "Wrong setup code" in r.text
    r = _setup(client, code)  # even the right code has to wait
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert auth.is_locked(conn)


def test_correct_code_sets_the_password_and_signs_in(fresh):
    client, conn, code, _ = fresh
    r = _setup(client, code.lower().replace("-", " "))  # forgiving about case, dash and spaces
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert auth.SESSION_COOKIE in client.cookies
    assert client.get("/admin/jobs").status_code == 200
    row = conn.execute("SELECT admin_password_source, admin_setup_code_hash FROM settings").fetchone()
    assert tuple(row) == ("ui", None)
    other = TestClient(client.app, headers={"Origin": ORIGIN})
    assert sign_in(other, "a long enough one").status_code == 303


def test_used_code_cannot_be_reused(fresh):
    client, conn, code, _ = fresh
    _setup(client, code)
    assert not auth.check_setup_code(conn, code)
    assert not auth.set_ui_password(conn, "something else entirely")
    r = _setup(TestClient(client.app, headers={"Origin": ORIGIN}), code, "another password")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert auth.check_password(conn, "a long enough one")


def test_short_or_mismatched_passwords_keep_the_code(fresh):
    client, conn, code, _ = fresh
    assert _setup(client, code, "short").status_code == 400
    r = _setup(client, code, "long enough", "different one")
    assert r.status_code == 400 and "not the same" in r.text
    assert auth.is_locked(conn) and auth.check_setup_code(conn, code)


def test_setup_post_needs_same_origin(fresh):
    _, conn, code, app = fresh
    r = _setup(TestClient(app), code)
    assert r.status_code == 403 and auth.is_locked(conn)
