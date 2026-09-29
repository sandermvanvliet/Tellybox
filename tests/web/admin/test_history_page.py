"""History page `/admin/history` (AD-4)."""

from __future__ import annotations

from tellybox import store
from tellybox.cast.controller import EndReason

from tests.web.conftest import NOW, PROFILE


def test_history_renders_with_cast_up(admin, admin_env):
    r = admin.get("/admin/history")
    assert r.status_code == 200
    assert "History" in r.text


def test_history_renders_with_cast_down(admin, admin_env):
    admin_env.cast.mode = "down"
    r = admin.get("/admin/history")
    assert r.status_code == 200


def test_history_shows_a_finished_episode(admin, admin_env):
    conn = admin_env.conn
    session_id = store.open_watch_session(conn, admin_env.ids.b1, [PROFILE], NOW)
    store.close_watch_session(conn, session_id, EndReason.FINISHED, NOW, 300.0)

    r = admin.get("/admin/history")
    assert r.status_code == 200
    assert "Title b1" in r.text
    assert "Bravo" in r.text
    assert "finished" in r.text.lower()


def test_history_shows_an_open_session_as_watching_now(admin, admin_env):
    conn = admin_env.conn
    store.open_watch_session(conn, admin_env.ids.b1, [PROFILE], NOW)

    r = admin.get("/admin/history")
    assert r.status_code == 200
    assert "watching now" in r.text.lower()


def test_history_shows_overrides(admin, admin_env):
    store.log_override(admin_env.conn, PROFILE, NOW.date(), "extra_minutes", 15, NOW)

    r = admin.get("/admin/history")
    assert r.status_code == 200
    assert "+15 min" in r.text


def _watched_by_two(env):
    conn = env.conn
    conn.execute("UPDATE profile SET name = 'Mila', avatar = 'fox' WHERE id = 1")
    conn.execute("INSERT INTO profile (id, name, avatar, sort_order, created_at) VALUES (2, 'Noor', 'owl', 2, 'x')")
    for ep, who in ((env.ids.b1, [1]), (env.ids.b2, [2]), (env.ids.a1, [1, 2])):
        sid = store.open_watch_session(conn, ep, who, NOW)
        store.close_watch_session(conn, sid, EndReason.FINISHED, NOW, 300.0)


def test_history_rows_show_who_watched(admin, admin_env):
    _watched_by_two(admin_env)
    text = admin.get("/admin/history").text
    assert "Mila" in text and "Noor" in text
    assert "/static/avatars/fox.svg" in text and "/static/avatars/owl.svg" in text


def test_history_profile_filter(admin, admin_env):
    _watched_by_two(admin_env)
    text = admin.get("/admin/history", params={"profile": 2}).text
    assert "Title b2" in text and "Title a1" in text and "Title b1" not in text
    assert "Title b1" in admin.get("/admin/history").text
    assert '<option value="2" selected>' in text


def test_history_profile_filter_ignores_garbage(admin, admin_env):
    _watched_by_two(admin_env)
    assert admin.get("/admin/history", params={"profile": "x"}).status_code == 200
    assert "Title b1" in admin.get("/admin/history", params={"profile": ""}).text


def test_history_overrides_name_their_profile(admin, admin_env):
    _watched_by_two(admin_env)
    store.log_override(admin_env.conn, 2, NOW.date(), "block", 1, NOW)
    assert "Noor" in admin.get("/admin/history").text.split("Overrides")[1]


def test_history_override_via_label(admin, admin_env):
    """HA-7: an override applied through an API token shows "via <name>"."""
    store.log_override(admin_env.conn, PROFILE, NOW.date(), "extra_minutes", 15, NOW)
    admin_env.conn.execute("UPDATE override_log SET source = ?", ("Home <b>Assistant</b>",))
    r = admin.get("/admin/history")
    assert "via Home &lt;b&gt;Assistant&lt;/b&gt;" in r.text


def test_history_override_without_source_has_no_via(admin, admin_env):
    store.log_override(admin_env.conn, PROFILE, NOW.date(), "extra_minutes", 15, NOW)
    assert "via " not in admin.get("/admin/history").text
