"""Admin side of show access (PR-5, AD-8, AD-9, HA-10): matrix, show checklist, approval-time grants,
copy-from on profile creation, the zero-show flag. Strict access: nothing is granted unless a test does it."""

from __future__ import annotations

import pytest

from tellybox import ingest, show_access, subscriptions
from tellybox.db import to_db
from tests.channel_fakes import FakeChannelLister
from tests.web.admin.test_add import FakeYtDlp, _preview, _preview_id, info
from tests.web.conftest import NOW

pytestmark = pytest.mark.strict_access

CHANNEL = "UCkids"


@pytest.fixture
def ytdlp():
    fake = FakeChannelLister()
    fake.add_channel(CHANNEL, "Kids Fun", handle="@kidsfun", videos=["old0"])
    fake.preview = FakeYtDlp().preview
    return fake


def second_profile(conn):
    conn.execute("INSERT INTO profile (id, name, sort_order, created_at) VALUES (2, 'Noor', 2, ?)", (to_db(NOW),))
    return 2


def granted(conn):
    return {(r[0], r[1]) for r in conn.execute("SELECT profile_id, show_id FROM profile_show")}


def test_the_library_fixture_starts_with_nothing_granted(admin_env):
    assert granted(admin_env.conn) == set()


# --------------------------------------------------------------------------- matrix


def test_matrix_page_lists_shows_and_profiles(admin, admin_env):
    second_profile(admin_env.conn)
    show_access.grant(admin_env.conn, 1, admin_env.ids.bravo)
    r = admin.get("/admin/access")
    assert r.status_code == 200
    assert 'value="1:%d" checked' % admin_env.ids.bravo in r.text
    assert 'value="2:%d" checked' % admin_env.ids.bravo not in r.text
    assert 'value="2:%d"' % admin_env.ids.bravo in r.text  # an unticked box
    assert 'href="/admin/access"' in r.text  # in the nav


def test_matrix_save_replaces_the_assignments(admin, admin_env):
    conn, ids = admin_env.conn, admin_env.ids
    second_profile(conn)
    show_access.grant(conn, 1, ids.bravo)
    r = admin.post("/admin/access", data={"cell": [f"2:{ids.bravo}"]}, follow_redirects=False)
    assert r.status_code == 303
    assert granted(conn) == {(2, ids.bravo)}


def test_matrix_save_ignores_unknown_ids_and_rejects_garbage(admin, admin_env):
    conn, ids = admin_env.conn, admin_env.ids
    r = admin.post("/admin/access", data={"cell": ["1:99999", "77:%d" % ids.bravo, f"1:{ids.bravo}"]},
                   follow_redirects=False)
    assert r.status_code == 303 and granted(conn) == {(1, ids.bravo)}
    assert admin.post("/admin/access", data={"cell": ["nope"]}, follow_redirects=False).status_code == 422
    assert granted(conn) == {(1, ids.bravo)}


def test_matrix_needs_sign_in_and_same_origin(anon, admin_env):
    r = anon.get("/admin/access", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login")
    r = anon.post("/admin/access", data={"cell": ["1:1"]}, headers={"Origin": "http://evil.example"})
    assert r.status_code in (401, 403)
    assert granted(admin_env.conn) == set()


def test_matrix_rejects_a_cross_origin_post_when_signed_in(admin, admin_env):
    r = admin.post("/admin/access", data={"cell": [f"1:{admin_env.ids.bravo}"]},
                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and granted(admin_env.conn) == set()


# --------------------------------------------------------------------------- show settings


def test_show_page_has_a_visible_to_checklist(admin, admin_env):
    second_profile(admin_env.conn)
    show_access.grant(admin_env.conn, 2, admin_env.ids.bravo)
    text = admin.get(f"/admin/shows/{admin_env.ids.bravo}").text
    assert "Visible to" in text
    assert 'name="profile" value="2" checked' in text
    assert 'name="profile" value="1" checked' not in text


def test_show_checklist_saves(admin, admin_env):
    conn, ids = admin_env.conn, admin_env.ids
    second_profile(conn)
    show_access.grant(conn, 1, ids.bravo)
    r = admin.post(f"/admin/shows/{ids.bravo}/access", data={"profile": [2, 99]}, follow_redirects=False)
    assert r.status_code == 303
    assert granted(conn) == {(2, ids.bravo)}
    admin.post(f"/admin/shows/{ids.bravo}/access", data={}, follow_redirects=False)
    assert granted(conn) == set()
    assert admin.post("/admin/shows/99999/access", data={"profile": [1]}).status_code == 404


# --------------------------------------------------------------------------- approval time


def test_add_by_url_offers_the_profiles_and_defaults_to_none(admin, admin_env):
    text = _preview(admin).text
    assert 'name="profile" value="1"' in text and 'name="profile" value="1" checked' not in text
    r = admin.post("/admin/add", data={"preview_id": _preview_id(text), "action": "add"}, follow_redirects=False)
    assert r.status_code == 303
    assert admin_env.conn.execute("SELECT COUNT(*) FROM source_video_profile").fetchone()[0] == 0


def test_add_by_url_remembers_the_chosen_profiles(admin, admin_env):
    second_profile(admin_env.conn)
    pid = _preview_id(_preview(admin).text)
    admin.post("/admin/add", data={"preview_id": pid, "action": "add", "profile": [2]}, follow_redirects=False)
    rows = admin_env.conn.execute("SELECT profile_id FROM source_video_profile").fetchall()
    assert [r[0] for r in rows] == [2]


def new_item(admin_env):
    sub = subscriptions.subscribe(admin_env.conn, admin_env.ytdlp, "https://www.youtube.com/@kidsfun", now=NOW)
    admin_env.ytdlp.upload(CHANNEL, "newA")
    assert subscriptions.check(admin_env.conn, admin_env.ytdlp, sub.id, now=NOW).ok
    return admin_env.conn.execute("SELECT id FROM inbox_item WHERE youtube_id = 'newA'").fetchone()[0]


def pending_profiles(conn):
    return [r[0] for r in conn.execute("SELECT profile_id FROM source_video_profile ORDER BY profile_id")]


def test_inbox_offers_the_checklist_unticked(admin, admin_env):
    new_item(admin_env)
    text = admin.get("/admin/inbox").text
    assert 'name="profile" value="1"' in text and 'checked' not in text.split('id="inbox-profiles"')[1].split("</details>")[0]


def test_inbox_approve_without_profiles_grants_nothing(admin, admin_env):  # A-9
    item = new_item(admin_env)
    r = admin.post(f"/admin/inbox/{item}/approve", data={"channel": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert pending_profiles(admin_env.conn) == [] and granted(admin_env.conn) == set()


def test_inbox_approve_remembers_the_profiles(admin, admin_env):
    second_profile(admin_env.conn)
    item = new_item(admin_env)
    admin.post(f"/admin/inbox/{item}/approve", data={"channel": "", "profile": [2]}, follow_redirects=False)
    assert pending_profiles(admin_env.conn) == [2]
    assert granted(admin_env.conn) == set()  # nothing granted until the show exists


def test_inbox_bulk_approve_remembers_the_profiles(admin, admin_env):
    item = new_item(admin_env)
    admin.post("/admin/inbox/bulk", data={"action": "approve", "item": [item], "profile": [1]}, follow_redirects=False)
    assert pending_profiles(admin_env.conn) == [1]


# --------------------------------------------------------------------------- profile creation


def test_new_profile_starts_with_no_shows(admin, admin_env):
    admin.post("/admin/profiles", data={"name": "Kit", "avatar": ""}, follow_redirects=False)
    new = admin_env.conn.execute("SELECT id FROM profile WHERE name = 'Kit'").fetchone()[0]
    assert show_access.visible_count_by_profile(admin_env.conn)[new] == 0


def test_copy_shows_from_is_a_one_off(admin, admin_env):
    conn, ids = admin_env.conn, admin_env.ids
    show_access.grant(conn, 1, ids.bravo)
    r = admin.post("/admin/profiles", data={"name": "Kit", "avatar": "", "copy_from": "1"}, follow_redirects=False)
    assert r.status_code == 303
    new = conn.execute("SELECT id FROM profile WHERE name = 'Kit'").fetchone()[0]
    assert show_access.visible_show_ids(conn, [new]) == {ids.bravo}
    show_access.revoke(conn, 1, ids.bravo)
    assert show_access.visible_show_ids(conn, [new]) == {ids.bravo}  # not inherited


def test_copy_from_an_unknown_profile_is_refused(admin, admin_env):
    r = admin.post("/admin/profiles", data={"name": "Kit", "avatar": "", "copy_from": "99"})
    assert r.status_code == 422
    assert admin_env.conn.execute("SELECT COUNT(*) FROM profile WHERE name = 'Kit'").fetchone()[0] == 0


def test_profiles_page_offers_copy_from_and_counts(admin, admin_env):
    show_access.grant(admin_env.conn, 1, admin_env.ids.bravo)
    text = admin.get("/admin/profiles").text
    assert 'name="copy_from"' in text and "1 show visible" in text


# --------------------------------------------------------------------------- dashboard flag and API


def test_dashboard_flags_a_profile_with_no_shows(admin, admin_env):
    assert "No shows visible" in admin.get("/admin").text
    show_access.grant(admin_env.conn, 1, admin_env.ids.bravo)
    assert "No shows visible" not in admin.get("/admin").text


def test_admin_state_carries_the_visible_show_count(admin_env):
    from tellybox.web.api.state import build_admin_state
    conn = admin_env.conn
    second_profile(conn)
    show_access.grant(conn, 1, admin_env.ids.bravo)
    state = build_admin_state(conn, admin_env.config, None, {"jobs": {}, "disk": {}}, NOW)
    assert {p["id"]: p["visible_shows"] for p in state["profiles"]} == {1: 1, 2: 0}


@pytest.mark.parametrize("method", ["put", "post", "patch", "delete"])
def test_the_admin_api_has_no_assignment_endpoints(admin_env, method):
    from fastapi.testclient import TestClient
    client = TestClient(admin_env.app)
    for path in ("/api/admin/access", "/api/admin/profiles/1/shows", "/api/admin/shows/1/profiles"):
        assert getattr(client, method)(path).status_code in (401, 404, 405)
