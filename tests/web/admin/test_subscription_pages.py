"""Subscriptions and inbox pages (CS-1..CS-9, A-31..A-36): rendering, guards, flows. Fake lister, fake cast."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tellybox import library, subscriptions
from tellybox.db import to_db
from tests.channel_fakes import FakeChannelLister
from tests.web.admin.conftest import ORIGIN

CHANNEL = "UCkids"
URL = "https://www.youtube.com/@kidsfun"


@pytest.fixture
def ytdlp():
    fake = FakeChannelLister()
    fake.add_channel(CHANNEL, "Kids Fun", handle="@kidsfun", videos=[f"old{i:03d}" for i in range(40)],
                     shorts=["oldshort0"])
    return fake


@pytest.fixture
def sub(admin_env):
    return subscriptions.subscribe(admin_env.conn, admin_env.ytdlp, URL, now=admin_env.clock.now())


def new_uploads(admin_env, sub, *ids, titles=None):
    """New uploads on the channel, checked into the inbox; returns the item ids."""
    for youtube_id in ids:
        admin_env.ytdlp.upload(CHANNEL, youtube_id)
    result = subscriptions.check(admin_env.conn, admin_env.ytdlp, sub.id, now=admin_env.clock.now())
    assert result.ok
    return result.new_item_ids


def item_ids(conn, *youtube_ids):
    return [conn.execute("SELECT id FROM inbox_item WHERE youtube_id = ?", (y,)).fetchone()[0] for y in youtube_ids]


def statuses(conn):
    return {r["youtube_id"]: r["status"] for r in conn.execute("SELECT youtube_id, status FROM inbox_item")}


# --------------------------------------------------------------------------- guards


@pytest.mark.parametrize("path", [
    "/admin/subscriptions", "/admin/inbox", "/admin/inbox/count", "/admin/subscriptions/1/backlog",
])
def test_pages_need_a_session(anon, path):
    r = anon.get(path, follow_redirects=False)
    assert r.status_code in (303, 401)


@pytest.mark.parametrize("path", [
    "/admin/subscriptions", "/admin/subscriptions/check-all", "/admin/subscriptions/1/check",
    "/admin/subscriptions/1/pause", "/admin/subscriptions/1/resume", "/admin/subscriptions/1/remove",
    "/admin/subscriptions/1/backlog", "/admin/inbox/1/approve", "/admin/inbox/1/reject", "/admin/inbox/1/undo",
    "/admin/inbox/bulk",
])
def test_posts_need_a_session_and_the_same_origin(admin_env, admin, path):
    anonymous = TestClient(admin_env.app, headers={"Origin": ORIGIN})
    assert anonymous.post(path, follow_redirects=False).status_code == 401
    foreign = TestClient(admin_env.app, headers={"Origin": "http://evil.example"})
    assert foreign.post(path, follow_redirects=False).status_code == 403
    # a signed-in client without an Origin header is refused as well
    admin.headers.pop("Origin")
    assert admin.post(path, follow_redirects=False).status_code == 403


# --------------------------------------------------------------------------- subscribe


def test_subscribe_page_lists_shows_and_subscriptions(admin, admin_env, sub):
    r = admin.get("/admin/subscriptions")
    assert r.status_code == 200
    assert "Kids Fun" in r.text and "Bravo" in r.text  # the show picker
    assert "Not checked yet" in r.text
    assert 'name="shorts"' in r.text


def test_subscribe_prefills_the_url_from_the_add_page(admin):
    r = admin.get("/admin/subscriptions", params={"url": URL})
    assert f'value="{URL}"' in r.text


def test_subscribe_creates_it_and_shows_the_backlog(admin, admin_env):
    r = admin.post("/admin/subscriptions", data={"url": URL, "show_id": str(admin_env.ids.alpha), "shorts": "1"},
                   follow_redirects=False)
    assert r.status_code == 303
    sub = subscriptions.list_subscriptions(admin_env.conn, now=admin_env.clock.now())[0]
    assert r.headers["location"] == f"/admin/subscriptions/{sub.id}/backlog"
    assert sub.show_id == admin_env.ids.alpha and sub.include_shorts and sub.channel_id == CHANNEL
    page = admin.get(r.headers["location"])
    assert page.status_code == 200 and "old000" in page.text


def test_subscribe_twice_is_refused(admin, sub):
    r = admin.post("/admin/subscriptions", data={"url": URL})
    assert r.status_code == 422 and "already subscribed" in r.text


def test_subscribe_to_a_bad_channel_shows_the_error(admin, admin_env):
    r = admin.post("/admin/subscriptions", data={"url": "https://www.youtube.com/@nobody"})
    assert r.status_code == 422 and "Channel not found" in r.text
    assert subscriptions.list_subscriptions(admin_env.conn, now=admin_env.clock.now()) == []


def test_subscribe_with_an_unknown_show_is_refused(admin, admin_env):
    r = admin.post("/admin/subscriptions", data={"url": URL, "show_id": "9999"})
    assert r.status_code == 422
    assert subscriptions.list_subscriptions(admin_env.conn, now=admin_env.clock.now()) == []


# --------------------------------------------------------------------------- backlog


def test_backlog_pages_30_at_a_time_and_skips_the_library(admin, admin_env, sub):
    admin_env.conn.execute(
        "INSERT INTO source_video (youtube_id, url, title, status, created_at, updated_at) VALUES"
        " ('old001', 'u', 't', 'ready', ?, ?)", (to_db(admin_env.clock.now()),) * 2)
    first = admin.get(f"/admin/subscriptions/{sub.id}/backlog")
    assert first.text.count('name="video"') == 29  # 30 listed minus the one already in the library
    assert 'value="old001"' not in first.text
    assert "Load more" in first.text and "offset=30" in first.text
    fragment = admin.get(f"/admin/subscriptions/{sub.id}/backlog", params={"offset": 30, "fragment": 1})
    assert fragment.text.count('name="video"') == 10 and "<html" not in fragment.text
    assert fragment.headers["x-next-offset"] == ""  # the listing ended


def test_backlog_approve_downloads_in_the_subscriptions_show(admin, admin_env, sub):
    conn = admin_env.conn
    conn.execute("UPDATE subscription SET show_id = ? WHERE id = ?", (admin_env.ids.bravo, sub.id))
    admin.get(f"/admin/subscriptions/{sub.id}/backlog")
    admin.get(f"/admin/subscriptions/{sub.id}/backlog", params={"offset": 30})
    r = admin.post(f"/admin/subscriptions/{sub.id}/backlog", data={"video": ["old002", "old035"]},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/jobs"
    rows = conn.execute("SELECT youtube_id, show_id, publish, status, channel_id FROM source_video "
                        "WHERE youtube_id IN ('old002', 'old035') ORDER BY youtube_id").fetchall()
    assert [tuple(x) for x in rows] == [("old002", admin_env.ids.bravo, "publish", "queued", CHANNEL),
                                        ("old035", admin_env.ids.bravo, "publish", "queued", CHANNEL)]
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM inbox_item").fetchone()[0] == 0  # the backlog never uses the inbox


def test_backlog_approve_skips_what_is_already_in_the_library(admin, admin_env, sub):
    admin.get(f"/admin/subscriptions/{sub.id}/backlog")
    admin.post(f"/admin/subscriptions/{sub.id}/backlog", data={"video": ["old002"]})
    r = admin.post(f"/admin/subscriptions/{sub.id}/backlog", data={"video": ["old002", "old003"]},
                   follow_redirects=False)
    assert r.status_code == 303
    assert admin_env.conn.execute("SELECT COUNT(*) FROM source_video WHERE youtube_id LIKE 'old00%'").fetchone()[0] == 2


def test_backlog_approve_refuses_ids_the_page_never_offered(admin, admin_env, sub):
    admin.get(f"/admin/subscriptions/{sub.id}/backlog")
    r = admin.post(f"/admin/subscriptions/{sub.id}/backlog", data={"video": ["old002", "old035"]},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/backlog")
    assert admin_env.conn.execute("SELECT COUNT(*) FROM source_video").fetchone()[0] == 0


def test_backlog_shows_the_shorts_tab_only_when_included(admin, admin_env, sub):
    assert "tab=shorts" not in admin.get(f"/admin/subscriptions/{sub.id}/backlog").text
    admin_env.conn.execute("UPDATE subscription SET include_shorts = 1 WHERE id = ?", (sub.id,))
    page = admin.get(f"/admin/subscriptions/{sub.id}/backlog", params={"tab": "shorts"})
    assert "tab=shorts" in page.text and "oldshort0" in page.text


def test_backlog_of_a_missing_subscription_goes_back_to_the_list(admin):
    r = admin.get("/admin/subscriptions/99/backlog", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/subscriptions"


def test_backlog_listing_failure_is_shown(admin, admin_env, sub):
    admin_env.ytdlp.fail = RuntimeError("boom")
    with pytest.raises(RuntimeError):  # not a YtDlpError: not ours to hide
        admin.get(f"/admin/subscriptions/{sub.id}/backlog")


# --------------------------------------------------------------------------- list, pause, check now, remove


def test_list_shows_state_error_and_warning_badge(admin, admin_env, sub):
    now = admin_env.clock.now()
    from datetime import timedelta
    admin_env.conn.execute(
        "UPDATE subscription SET last_checked_at = ?, last_error = ?, failing_since = ?, paused = 1 WHERE id = ?",
        (to_db(now), "HTTP Error 404 <b>", to_db(now - timedelta(days=8)), sub.id))
    page = admin.get("/admin/subscriptions").text
    assert "Failing for 7 days or more" in page
    assert "HTTP Error 404 &lt;b&gt;" in page and "<b>" not in page  # the error is escaped
    assert "paused" in page and "Resume" in page and "Last checked:" in page


def test_pause_resume_and_remove(admin, admin_env, sub):
    conn = admin_env.conn
    assert admin.post(f"/admin/subscriptions/{sub.id}/pause", follow_redirects=False).status_code == 303
    assert subscriptions.get_subscription(conn, sub.id).paused
    admin.post(f"/admin/subscriptions/{sub.id}/resume")
    assert not subscriptions.get_subscription(conn, sub.id).paused
    assert 'data-confirm="Remove this subscription?' in admin.get("/admin/subscriptions").text
    admin.post(f"/admin/subscriptions/{sub.id}/remove")
    assert subscriptions.get_subscription(conn, sub.id) is None


def test_actions_on_a_missing_subscription_do_not_fail(admin):
    for action in ("pause", "resume", "remove", "check"):
        r = admin.post(f"/admin/subscriptions/99/{action}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/subscriptions"


def test_check_now_asks_the_worker_for_one_or_all(admin, admin_env, sub):
    conn, now = admin_env.conn, admin_env.clock.now()
    other = subscriptions.subscribe(conn, _second_channel(admin_env), "https://www.youtube.com/@other", now=now)
    conn.execute("UPDATE subscription SET last_checked_at = ?", (to_db(now),))
    assert subscriptions.due_subscriptions(conn, now) == []
    admin.post(f"/admin/subscriptions/{sub.id}/check")
    assert subscriptions.due_subscriptions(conn, now) == [sub.id]
    admin.post("/admin/subscriptions/check-all")
    assert sorted(subscriptions.due_subscriptions(conn, now)) == sorted([sub.id, other.id])
    # the page itself never talks to YouTube: the worker does the check
    assert admin_env.ytdlp.status_calls == []


def _second_channel(admin_env):
    admin_env.ytdlp.add_channel("UCother", "Other", handle="@other", videos=["x1"])
    return admin_env.ytdlp


def test_check_all_skips_paused_but_check_now_does_not(admin, admin_env, sub):
    conn, now = admin_env.conn, admin_env.clock.now()
    conn.execute("UPDATE subscription SET paused = 1, last_checked_at = ? WHERE id = ?", (to_db(now), sub.id))
    admin.post("/admin/subscriptions/check-all")
    assert subscriptions.due_subscriptions(conn, now) == []
    admin.post(f"/admin/subscriptions/{sub.id}/check")
    assert subscriptions.due_subscriptions(conn, now) == [sub.id]


# --------------------------------------------------------------------------- inbox


def test_inbox_lists_pending_newest_first_with_details(admin, admin_env, sub):
    new_uploads(admin_env, sub, "newA")
    admin_env.conn.execute("UPDATE inbox_item SET title = ?, published_at = ? WHERE youtube_id = 'newA'",
                           ("<script>alert(1)</script> Bluey", "2026-09-01T12:00:00+00:00"))
    new_uploads(admin_env, sub, "newB")
    admin_env.conn.execute("UPDATE inbox_item SET published_at = '2026-09-20T12:00:00+00:00' WHERE youtube_id = 'newB'")
    page = admin.get("/admin/inbox").text
    assert page.index("newB") < page.index("newA") or page.index("Video newB") < page.index("Bluey")
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Bluey" in page and "<script>alert" not in page
    assert "Kids Fun" in page and "5:00" in page and "Open on YouTube" in page
    assert 'href="https://www.youtube.com/watch?v=newA"' in page
    assert 'value="newA"' not in page  # checkboxes carry item ids, not video ids


def test_inbox_translates_the_warning_and_escapes_the_channel(admin, admin_env, sub):
    [item_id] = new_uploads(admin_env, sub, "newA")
    admin_env.conn.execute("UPDATE inbox_item SET warning = ?, channel_name = ? WHERE id = ?",
                           (subscriptions.WARNING_NOT_DOWNLOADABLE, "<i>Kids</i>", item_id))
    page = admin.get("/admin/inbox", headers={"Accept-Language": "nl"}).text
    assert "is misschien niet te downloaden" in page and "may not be downloadable" not in page
    assert "&lt;i&gt;Kids&lt;/i&gt;" in page and "<i>Kids</i>" not in page


def test_inbox_channel_filter(admin, admin_env, sub):
    new_uploads(admin_env, sub, "newA")
    other = subscriptions.subscribe(admin_env.conn, _second_channel(admin_env), "https://www.youtube.com/@other",
                                    now=admin_env.clock.now())
    admin_env.ytdlp.upload("UCother", "otherNew")
    subscriptions.check(admin_env.conn, admin_env.ytdlp, other.id, now=admin_env.clock.now())
    both = admin.get("/admin/inbox").text
    assert "Video newA" in both and "Video otherNew" in both
    only = admin.get("/admin/inbox", params={"channel": other.id}).text
    assert "Video otherNew" in only and "Video newA" not in only


def test_approve_downloads_publishes_and_follows_the_subscriptions_show(admin, admin_env, sub):
    conn = admin_env.conn
    conn.execute("UPDATE subscription SET show_id = ? WHERE id = ?", (admin_env.ids.alpha, sub.id))
    [item_id] = new_uploads(admin_env, sub, "newA")
    r = admin.post(f"/admin/inbox/{item_id}/approve", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/inbox"
    row = conn.execute("SELECT show_id, publish, status FROM source_video WHERE youtube_id = 'newA'").fetchone()
    assert tuple(row) == (admin_env.ids.alpha, "publish", "queued")
    assert statuses(conn)["newA"] == "approved"
    assert "Approved 1 video" in admin.get("/admin/inbox").text  # the flash
    # a second click is harmless
    again = admin.post(f"/admin/inbox/{item_id}/approve", follow_redirects=True)
    assert "already decided" in again.text


def test_reject_and_undo_from_the_rejected_tab(admin, admin_env, sub):
    [item_id] = new_uploads(admin_env, sub, "newA")
    admin.post(f"/admin/inbox/{item_id}/reject")
    assert statuses(admin_env.conn)["newA"] == "rejected"
    assert "Video newA" not in admin.get("/admin/inbox").text
    rejected = admin.get("/admin/inbox", params={"tab": "rejected"}).text
    assert "Video newA" in rejected and f"/admin/inbox/{item_id}/undo" in rejected
    r = admin.post(f"/admin/inbox/{item_id}/undo", follow_redirects=False)
    assert r.headers["location"] == "/admin/inbox?tab=rejected"
    assert statuses(admin_env.conn)["newA"] == "pending"


def test_bulk_approve_and_reject_selected(admin, admin_env, sub):
    new_uploads(admin_env, sub, "newA", "newB", "newC")
    a, b, c = item_ids(admin_env.conn, "newA", "newB", "newC")
    admin.post("/admin/inbox/bulk", data={"action": "approve", "item": [a, b]})
    admin.post("/admin/inbox/bulk", data={"action": "reject", "item": [c]})
    assert statuses(admin_env.conn) == {"newA": "approved", "newB": "approved", "newC": "rejected"}
    assert admin_env.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2


def test_bulk_with_nothing_ticked_changes_nothing(admin, admin_env, sub):
    new_uploads(admin_env, sub, "newA")
    r = admin.post("/admin/inbox/bulk", data={"action": "approve"}, follow_redirects=True)
    assert "Tick at least one video" in r.text
    assert set(statuses(admin_env.conn).values()) == {"pending"}


def test_reject_all_remaining_respects_the_channel_filter(admin, admin_env, sub):
    new_uploads(admin_env, sub, "newA", "newB")
    other = subscriptions.subscribe(admin_env.conn, _second_channel(admin_env), "https://www.youtube.com/@other",
                                    now=admin_env.clock.now())
    admin_env.ytdlp.upload("UCother", "otherNew")
    subscriptions.check(admin_env.conn, admin_env.ytdlp, other.id, now=admin_env.clock.now())
    admin.post("/admin/inbox/bulk", data={"action": "reject_all", "channel": str(sub.id)})
    assert statuses(admin_env.conn) == {"newA": "rejected", "newB": "rejected", "otherNew": "pending"}
    admin.post("/admin/inbox/bulk", data={"action": "reject_all"})
    assert set(statuses(admin_env.conn).values()) == {"rejected"}


def test_badge_count_in_the_nav_and_the_poll_endpoint(admin, admin_env, sub):
    assert 'id="inbox-badge" hidden' in admin.get("/admin").text
    new_uploads(admin_env, sub, "newA", "newB")
    page = admin.get("/admin/subscriptions").text
    assert 'id="inbox-badge">2<' in page
    assert admin.get("/admin/inbox/count").json() == {"pending": 2}
    admin.post("/admin/inbox/bulk", data={"action": "reject_all"})
    assert admin.get("/admin/inbox/count").json() == {"pending": 0}


def test_inbox_survives_removing_the_subscription(admin, admin_env, sub):
    new_uploads(admin_env, sub, "newA", "newB")
    [a] = item_ids(admin_env.conn, "newA")
    admin.post(f"/admin/inbox/{a}/reject")
    admin.post(f"/admin/subscriptions/{sub.id}/remove")
    assert statuses(admin_env.conn) == {"newA": "rejected"}  # pending dropped, the rejection remembered
    assert "Video newA" in admin.get("/admin/inbox", params={"tab": "rejected"}).text


# --------------------------------------------------------------------------- settings


def test_settings_check_interval(admin, admin_env):
    conn = admin_env.conn
    page = admin.get("/admin/settings").text
    assert 'name="subscription_check_hours"' in page and 'value="6"' in page
    form = _settings_form(conn)
    r = admin.post("/admin/settings", data={**form, "subscription_check_hours": "12"}, follow_redirects=False)
    assert r.status_code == 303 and subscriptions.get_check_hours(conn) == 12
    for bad in ("0", "-1", "x", ""):
        r = admin.post("/admin/settings", data={**form, "subscription_check_hours": bad})
        assert r.status_code == 422, bad
    assert subscriptions.get_check_hours(conn) == 12
    # a form without the field leaves it alone
    admin.post("/admin/settings", data=form)
    assert subscriptions.get_check_hours(conn) == 12


def _settings_form(conn) -> dict:
    s = conn.execute("SELECT reset_time, grace_cap_min, session_break_min, default_allowance_min, "
                     "default_max_session_min FROM settings WHERE id = 1").fetchone()
    form = {"reset_time": s["reset_time"], "grace_cap_min": s["grace_cap_min"],
            "session_break_min": s["session_break_min"], "default_allowance_min": s["default_allowance_min"],
            "default_max_session_min": s["default_max_session_min"]}
    for p in conn.execute("SELECT id, allowance_mode, daily_allowance_min, counting_mode, max_session_mode, "
                          "max_session_min FROM profile"):
        i = p["id"]
        form.update({f"profile_{i}_allowance_mode": p["allowance_mode"],
                     f"profile_{i}_allowance_min": p["daily_allowance_min"],
                     f"profile_{i}_counting_mode": p["counting_mode"],
                     f"profile_{i}_max_session_mode": p["max_session_mode"],
                     f"profile_{i}_max_session_min": p["max_session_min"]})
    return form


# --------------------------------------------------------------------------- the forced show is not "published"


def test_a_queued_download_in_a_forced_show_is_not_published_there(admin_env, sub):
    conn = admin_env.conn
    conn.execute("UPDATE subscription SET show_id = ? WHERE id = ?", (admin_env.ids.empty, sub.id))
    media = admin_env.config.media_dir

    def view():
        items, total = library.list_shows_for_admin(conn, media)
        return {i.show.id: (i.episode_count, i.hidden_episode_count, i.disk_bytes) for i in items}, total

    before = view()
    [item_id] = new_uploads(admin_env, sub, "newA")
    subscriptions.approve(conn, item_id, now=admin_env.clock.now())
    assert conn.execute("SELECT show_id FROM source_video WHERE youtube_id = 'newA'").fetchone()[0] == admin_env.ids.empty
    assert view() == before  # no new episode and no bytes in the forced show until the download publishes
    assert library.count_held_ready(conn) == 0 and library.list_held_downloads(conn) == []
