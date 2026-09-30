"""SponsorBlock in the admin (SB-2, SB-4): settings, the show override, the episode page and the
"download again" buttons, and the job labels."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from tellybox import jobs, library, sponsorblock
from tellybox.db import to_db
from tellybox.jobs import JobType
from tests.web.admin.test_settings import VALID_FORM
from tests.web.conftest import NOW

SEGMENTS = [
    sponsorblock.Segment("sponsor", 62.0, 95.0),
    sponsorblock.Segment("selfpromo", 300.0, 312.5),
]


def _source(env, *, status="cut", ready=True, segments=(), window_days=5, split=False, show=None):
    """A published source video with one episode; returns (source_id, episode_id)."""
    conn = env.conn
    show = show or env.ids.alpha
    removed = sponsorblock.removed_s(segments) if segments else None
    cur = conn.execute(
        """INSERT INTO source_video (youtube_id, url, title, show_id, file_path, publish, status, created_at, updated_at,
                                     sb_status, sb_segments_json, sb_removed_s, sb_checked_at, sb_recheck_until)
           VALUES ('sbvid', 'https://youtu.be/sbvid', 'SB video', ?, 'eps/sb.mp4', 'publish', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (show, "ready" if ready else "downloading", to_db(NOW), to_db(NOW), status,
         sponsorblock.dumps(segments) if segments else None, removed, to_db(NOW),
         to_db(NOW + timedelta(days=window_days))),
    )
    sid = cur.lastrowid
    eid = library.add_episode(conn, show, "SB episode", "eps/sb.mp4", now=NOW, source_video_id=sid, duration_s=500.0)
    if split:
        conn.execute("UPDATE episode SET start_s = 0, end_s = 100 WHERE id = ?", (eid,))
    return sid, eid


def _page(client, url) -> str:
    """The page's HTML without the script blocks (the JS string catalog is on every page)."""
    return re.sub(r"<script.*?</script>", "", client.get(url).text, flags=re.S)


def _saved(admin_env, column="sponsorblock_categories", table="settings"):
    return admin_env.conn.execute(f"SELECT {column} FROM {table}").fetchone()[0]


# --------------------------------------------------------------------------- settings


def test_settings_lists_every_category_with_the_defaults_ticked(admin):
    html = admin.get("/admin/settings").text
    for key in sponsorblock.CATEGORIES:
        assert f'name="sponsorblock" value="{key}"' in html
    assert 'value="sponsor" checked' in html
    assert 'value="intro" checked' not in html
    assert "Unpaid or self promotion" in html


def test_settings_save_stores_the_ticked_categories(admin, admin_env):
    r = admin.post("/admin/settings", data={**VALID_FORM, "sponsorblock": ["outro", "sponsor", "bogus"]},
                   follow_redirects=False)
    assert r.status_code == 303
    assert _saved(admin_env) == "sponsor,outro"
    html = admin.get("/admin/settings").text
    assert 'value="outro" checked' in html
    assert 'value="selfpromo" checked' not in html


def test_settings_save_with_nothing_ticked_turns_it_off(admin, admin_env):
    admin.post("/admin/settings", data=VALID_FORM, follow_redirects=False)
    assert _saved(admin_env) == ""
    fieldset = admin.get("/admin/settings").text.split('name="sponsorblock"', 1)[1].split("</fieldset>")[0]
    assert "checked" not in fieldset


def test_settings_error_keeps_the_ticked_boxes_and_does_not_save(admin, admin_env):
    r = admin.post("/admin/settings", data={**VALID_FORM, "reset_time": "nope", "sponsorblock": ["hook"]})
    assert r.status_code == 422
    assert 'value="hook" checked' in r.text
    assert _saved(admin_env) == "sponsor,selfpromo,interaction"


# --------------------------------------------------------------------------- show override


def test_show_page_offers_default_with_its_categories(admin, admin_env):
    html = admin.get(f"/admin/shows/{admin_env.ids.alpha}").text
    assert "Use the default (Sponsor, Unpaid or self promotion, Interaction reminder (subscribe))" in html
    assert 'value="default" checked' in html


def test_show_override_choose_off_and_default(admin, admin_env):
    conn, sid = admin_env.conn, admin_env.ids.alpha
    url = f"/admin/shows/{sid}/sponsorblock"

    r = admin.post(url, data={"mode": "choose", "category": ["intro", "sponsor"]}, follow_redirects=False)
    assert r.status_code == 303
    assert library.get_show(conn, sid).sponsorblock_categories == "sponsor,intro"
    assert sponsorblock.effective_categories(conn, sid) == ["sponsor", "intro"]
    html = admin.get(f"/admin/shows/{sid}").text
    assert 'value="choose" checked' in html and 'value="intro" checked' in html

    admin.post(url, data={"mode": "off"})
    assert library.get_show(conn, sid).sponsorblock_categories == ""
    assert sponsorblock.effective_categories(conn, sid) == []
    assert 'value="off" checked' in admin.get(f"/admin/shows/{sid}").text

    admin.post(url, data={"mode": "choose"})  # nothing ticked: off
    assert library.get_show(conn, sid).sponsorblock_categories == ""

    admin.post(url, data={"mode": "default"})
    assert library.get_show(conn, sid).sponsorblock_categories is None


def test_show_override_rejects_bad_mode_and_unknown_show(admin, admin_env):
    assert admin.post(f"/admin/shows/{admin_env.ids.alpha}/sponsorblock", data={"mode": "x"}).status_code == 422
    assert admin.post("/admin/shows/9999/sponsorblock", data={"mode": "off"}).status_code == 404


def test_show_page_links_to_the_episode_page(admin, admin_env):
    assert f'href="/admin/episodes/{admin_env.ids.a1}"' in admin.get(f"/admin/shows/{admin_env.ids.alpha}").text


# --------------------------------------------------------------------------- episode page


def test_episode_page_without_a_source_video_has_no_sponsorblock_section(admin, admin_env):
    assert admin.get(f"/admin/episodes/{admin_env.ids.a1}").status_code == 200
    html = _page(admin, f"/admin/episodes/{admin_env.ids.a1}")
    assert "Title a1" in html and "Alpha" in html
    assert "SponsorBlock" not in html


def test_episode_page_unknown_is_404(admin):
    assert admin.get("/admin/episodes/9999").status_code == 404


def test_episode_page_cut_lists_segments_total_and_buttons(admin, admin_env):
    _, eid = _source(admin_env, status="cut", segments=SEGMENTS)
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "Sponsor" in html and "Unpaid or self promotion" in html
    assert "1:02" in html and "1:35" in html and "5:00" in html and "5:12" in html
    assert "Removed in total: 0:46" in html  # 33 s + 12.5 s
    assert "Checked daily until" in html
    assert "Download again without SponsorBlock" in html
    assert "Download again with SponsorBlock" not in html


def test_episode_page_none(admin, admin_env):
    _, eid = _source(admin_env, status="none")
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "No segments found." in html and "Checked daily until" in html
    assert "Download again" not in html


def test_episode_page_unreachable(admin, admin_env):
    _, eid = _source(admin_env, status="unreachable")
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "be reached; it will be checked again." in html


def test_episode_page_off_for_the_show(admin, admin_env):
    _, eid = _source(admin_env, status="off")
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "SponsorBlock is off for this show." in html
    assert "Download again" not in html


def test_episode_page_admin_off_offers_download_with(admin, admin_env):
    _, eid = _source(admin_env, status="admin_off")
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "Downloaded again without SponsorBlock." in html
    assert "Download again with SponsorBlock" in html
    assert "Download again without SponsorBlock" not in html


def test_episode_page_pre_v3_shows_only_the_button(admin, admin_env):
    _, eid = _source(admin_env, status=None, window_days=-1)
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "Download again with SponsorBlock" in html
    assert "Checked daily until" not in html and "No segments found" not in html


def test_episode_page_window_passed_hides_the_daily_check_line(admin, admin_env):
    _, eid = _source(admin_env, status="cut", segments=SEGMENTS, window_days=-1)
    assert "Checked daily until" not in _page(admin, f"/admin/episodes/{eid}")


def test_episode_page_pending_redownload_shows_no_buttons(admin, admin_env):
    sid, eid = _source(admin_env, status="cut", segments=SEGMENTS)
    jobs.enqueue(admin_env.conn, JobType.REDOWNLOAD, sid, now=NOW)
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "Being downloaded again" in html
    assert "Download again with" not in html and "Download again without" not in html


def test_episode_page_split_video_has_no_buttons(admin, admin_env):
    _, eid = _source(admin_env, status="cut", segments=SEGMENTS, split=True)
    html = _page(admin, f"/admin/episodes/{eid}")
    assert "be downloaded again" in html
    assert f'action="/admin/episodes/{eid}/redownload"' not in html


# --------------------------------------------------------------------------- redownload


def test_redownload_without_sponsorblock_queues_a_job_and_marks_admin_off(admin, admin_env):
    sid, eid = _source(admin_env, status="cut", segments=SEGMENTS)
    r = admin.post(f"/admin/episodes/{eid}/redownload", data={"sponsorblock": "0"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/episodes/{eid}"
    assert _saved(admin_env, "sb_status", "source_video") == "admin_off"
    queued = jobs.list_jobs(admin_env.conn, limit=10)
    assert [(j.type, j.target_id) for j in queued] == [(JobType.REDOWNLOAD, sid)]
    assert "downloaded again" in _page(admin, r.headers["location"])


def test_redownload_with_sponsorblock_clears_admin_off(admin, admin_env):
    sid, eid = _source(admin_env, status="admin_off", window_days=-1)
    admin.post(f"/admin/episodes/{eid}/redownload", data={"sponsorblock": "1"})
    row = admin_env.conn.execute("SELECT sb_status, sb_recheck_until FROM source_video WHERE id = ?", (sid,)).fetchone()
    assert row["sb_status"] is None
    assert row["sb_recheck_until"] > to_db(NOW)
    assert jobs.has_pending_for(admin_env.conn, sid, (JobType.REDOWNLOAD,))


def test_redownload_refused_while_one_is_pending(admin, admin_env):
    sid, eid = _source(admin_env, status="cut", segments=SEGMENTS)
    jobs.enqueue(admin_env.conn, JobType.REDOWNLOAD, sid, now=NOW)
    r = admin.post(f"/admin/episodes/{eid}/redownload", data={"sponsorblock": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert "already being checked" in admin.get(r.headers["location"]).text
    assert len(jobs.list_jobs(admin_env.conn, limit=10)) == 1
    assert _saved(admin_env, "sb_status", "source_video") == "cut"


def test_redownload_of_an_unpublished_video_is_a_message(admin, admin_env):
    _, eid = _source(admin_env, status="cut", ready=False)
    r = admin.post(f"/admin/episodes/{eid}/redownload", data={"sponsorblock": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert "downloaded again right now" in admin.get(r.headers["location"]).text
    assert jobs.list_jobs(admin_env.conn, limit=10) == []


def test_redownload_of_a_manual_episode_is_a_message(admin, admin_env):
    eid = admin_env.ids.a1
    r = admin.post(f"/admin/episodes/{eid}/redownload", data={"sponsorblock": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert "no downloaded video" in admin.get(r.headers["location"]).text


def test_redownload_validates_the_form(admin, admin_env):
    eid = admin_env.ids.a1
    assert admin.post(f"/admin/episodes/{eid}/redownload", data={"sponsorblock": "2"}).status_code == 422
    assert admin.post("/admin/episodes/9999/redownload", data={"sponsorblock": "0"}).status_code == 404


def test_redownload_needs_a_signed_in_session(anon, admin_env):
    r = anon.post(f"/admin/episodes/{admin_env.ids.a1}/redownload", data={"sponsorblock": "0"}, follow_redirects=False)
    assert r.status_code == 401


# --------------------------------------------------------------------------- jobs page


@pytest.mark.parametrize("job_type,label", [(JobType.SB_RECHECK, "Check SponsorBlock"),
                                            (JobType.REDOWNLOAD, "Download again")])
def test_jobs_page_labels_sponsorblock_jobs_with_the_video_title(admin, admin_env, job_type, label):
    sid, _ = _source(admin_env)
    job_id = jobs.enqueue(admin_env.conn, job_type, sid, now=NOW)
    html = admin.get("/admin/jobs").text
    assert "SB video" in html and label in html
    (row,) = [r for r in admin.get("/admin/api/jobs").json() if r["id"] == job_id]
    assert row["label"] == "SB video"
