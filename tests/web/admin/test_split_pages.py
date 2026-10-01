"""Manual splitting in the admin (ES-1, ES-2, ES-7): the split page and its API, the source video
and frame routes, the links on the episode and library pages, and the job labels."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tellybox import db, jobs, library, splitting
from tellybox.jobs import JobType
from tests.web.admin.test_library_pages import _ffmpeg
from tests.web.conftest import NOW

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)
DURATION = 20.0
CHAPTERS = [{"start_s": 0, "end_s": 8, "title": "One"}, {"start_s": 8, "end_s": 20, "title": "Two"}]


def _source(env, *, file=True, status="ready", chapters=None, title="Long video", duration=DURATION, ytid="longvid"):
    """A ready source video with one episode (the whole video); returns (source_id, episode_id)."""
    conn, rel = env.conn, "eps/long.mp4"
    if file:
        path = env.config.media_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=5:duration={int(duration)}",
                "-c:v", "libx264", "-preset", "superfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path))
    cur = conn.execute(
        """INSERT INTO source_video (youtube_id, url, title, show_id, file_path, publish, status, duration_s,
                                     chapters_json, created_at, updated_at)
           VALUES (?, 'https://youtu.be/x', ?, ?, ?, 'publish', ?, ?, ?, ?, ?)""",
        (ytid, title, env.ids.alpha, rel if file else None, status, duration,
         json.dumps(chapters) if chapters else None, db.to_db(NOW), db.to_db(NOW)),
    )
    sid = cur.lastrowid
    eid = library.add_episode(conn, env.ids.alpha, title, rel, now=NOW, source_video_id=sid, duration_s=duration)
    return sid, eid


def _body(page: str) -> str:
    return re.sub(r"<script.*?</script>", "", page, flags=re.S)


def _plan(*cuts, titles=()):
    segs = splitting.segments_from_cuts(DURATION, cuts, titles)
    return [{"start_s": s.start_s, "end_s": s.end_s, "title": s.title, "keep": s.keep} for s in segs]


# --------------------------------------------------------------------------- source video and frame


@needs_ffmpeg
def test_video_route_serves_ranges(admin, admin_env):
    sid, _ = _source(admin_env)
    r = admin.get(f"/admin/sources/{sid}/video.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert len(r.content) == 100
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "video/mp4"


@needs_ffmpeg
def test_video_and_frame_are_404_once_the_file_is_gone(admin, admin_env):
    sid, _ = _source(admin_env)
    admin_env.conn.execute("UPDATE source_video SET file_path = NULL WHERE id = ?", (sid,))
    assert admin.get(f"/admin/sources/{sid}/video.mp4").status_code == 404
    assert admin.get(f"/admin/sources/{sid}/frame.jpg?t=1").status_code == 404
    assert admin.get("/admin/sources/99999/video.mp4").status_code == 404


@needs_ffmpeg
def test_frame_route_returns_a_jpeg(admin, admin_env):
    sid, _ = _source(admin_env)
    r = admin.get(f"/admin/sources/{sid}/frame.jpg?t=3.2")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"


def test_source_routes_need_a_session(anon):
    assert anon.get("/admin/sources/1/video.mp4", headers={"accept": "*/*"}).status_code == 401
    assert anon.get("/admin/api/splits/1", headers={"accept": "application/json"}).status_code == 401


# --------------------------------------------------------------------------- state API


@needs_ffmpeg
def test_get_state_from_chapters(admin, admin_env):
    sid, _ = _source(admin_env, chapters=CHAPTERS)
    state = admin.get(f"/admin/api/splits/{sid}").json()
    assert state["source_id"] == sid
    assert state["duration_s"] == DURATION
    assert state["fps"] == 5.0
    assert state["video_url"] == f"/admin/sources/{sid}/video.mp4"
    assert state["frame_url"] == f"/admin/sources/{sid}/frame.jpg"
    assert (state["status"], state["origin"], state["stored"]) == ("draft", "chapters", False)
    assert state["editable"] and state["has_file"]
    assert [(s["start_s"], s["title"]) for s in state["segments"]] == [(0.0, "One"), (8.0, "Two")]
    assert [c["title"] for c in state["chapters"]] == ["One", "Two"]
    assert state["job"] is None and state["error"] is None


def test_get_state_of_an_unknown_or_unready_source_is_404(admin, admin_env):
    sid, _ = _source(admin_env, file=False, status="downloading")
    assert admin.get(f"/admin/api/splits/{sid}").status_code == 404
    assert admin.get("/admin/api/splits/99999").status_code == 404


@needs_ffmpeg
def test_put_saves_a_draft_and_returns_the_state(admin, admin_env):
    sid, _ = _source(admin_env)
    r = admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(5, 12, titles=["A", "B", "C"]), "origin": "manual"})
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["stored"] and state["status"] == "draft"
    assert [s["title"] for s in state["segments"]] == ["A", "B", "C"]
    assert admin.get(f"/admin/api/splits/{sid}").json()["segments"] == state["segments"]
    assert library.get_split(admin_env.conn, sid).stored


@needs_ffmpeg
def test_put_rejects_a_malformed_plan_with_422(admin, admin_env):
    sid, _ = _source(admin_env)
    r = admin.put(f"/admin/api/splits/{sid}", json={"segments": [{"start_s": 0, "end_s": 5}]})  # doesn't cover it
    assert r.status_code == 422
    assert r.json()["error"]
    assert admin.put(f"/admin/api/splits/{sid}", json={"segments": [{"nope": 1}]}).status_code == 422
    assert admin.put(f"/admin/api/splits/{sid}", json={"nothing": 1}).status_code == 422
    assert admin.put(f"/admin/api/splits/{sid}", content=b"not json").status_code == 422
    r = admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(5), "origin": "bogus"})
    assert r.status_code == 422
    assert not library.get_split(admin_env.conn, sid).stored


@needs_ffmpeg
def test_put_conflicts_while_the_job_runs_and_when_the_file_is_gone(admin, admin_env):
    sid, _ = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(10)})
    library.approve_split(admin_env.conn, sid, delete_source=False, now=NOW)
    r = admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(5)})
    assert r.status_code == 409 and r.json()["error"]
    job = admin.get(f"/admin/api/splits/{sid}").json()["job"]
    assert job["status"] == "queued"

    sid2, _ = _source(admin_env, file=False, ytid="other")
    assert admin.put(f"/admin/api/splits/{sid2}", json={"segments": _plan(5)}).status_code == 409
    assert admin.put("/admin/api/splits/99999", json={"segments": _plan(5)}).status_code == 404


@needs_ffmpeg
def test_put_needs_the_origin_header(admin, admin_env):
    sid, _ = _source(admin_env)
    r = admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(5)}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert not library.get_split(admin_env.conn, sid).stored


# --------------------------------------------------------------------------- the page


@needs_ffmpeg
def test_split_page_renders_the_editor_hooks_and_the_state(admin, admin_env):
    sid, _ = _source(admin_env, chapters=CHAPTERS)
    html = admin.get(f"/admin/sources/{sid}/split").text
    page = _body(html)
    for hook in ('id="split-editor"', 'data-editable="true"', "data-split-video", "data-split-time",
                 'data-action="cut"', 'data-action="use-chapters"', "data-split-segments", "data-split-strip",
                 "data-split-status", "data-split-approve", "data-split-estimate", 'name="delete_source"'):
        assert hook in page, hook
    for step in ("-10", "-1", "-frame", "+frame", "+1", "+10"):
        assert f'data-step="{step}"' in page
    assert f'src="/admin/sources/{sid}/video.mp4"' in page
    assert "/admin/static/split.css" in html and "/admin/static/split.js" in html
    state = json.loads(re.search(r'<script type="application/json" id="split-state">(.*?)</script>', html, re.S).group(1))
    assert state["source_id"] == sid and len(state["segments"]) == 2
    assert "data-split-discard" not in page  # nothing saved yet


@needs_ffmpeg
def test_split_page_hides_use_chapters_without_chapters(admin, admin_env):
    sid, _ = _source(admin_env)
    assert 'data-action="use-chapters"' not in admin.get(f"/admin/sources/{sid}/split").text


@needs_ffmpeg
def test_split_page_shows_discard_for_a_saved_draft(admin, admin_env):
    sid, _ = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(10)})
    assert "data-split-discard" in admin.get(f"/admin/sources/{sid}/split").text


def test_split_page_404s(admin, admin_env):
    sid, _ = _source(admin_env, file=False, status="downloading")
    assert admin.get(f"/admin/sources/{sid}/split").status_code == 404
    assert admin.get("/admin/sources/99999/split").status_code == 404


def test_split_page_says_so_when_the_original_is_gone(admin, admin_env):
    sid, _ = _source(admin_env, file=False)
    page = _body(admin.get(f"/admin/sources/{sid}/split").text)
    assert "was deleted after splitting" in page
    assert "split-editor" not in page


@needs_ffmpeg
def test_approve_queues_the_job_and_flashes(admin, admin_env):
    sid, _ = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(10)})
    r = admin.post(f"/admin/sources/{sid}/split/approve", data={"delete_source": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/sources/{sid}/split"
    assert jobs.has_pending_for(admin_env.conn, sid, [JobType.SPLIT])
    split = library.get_split(admin_env.conn, sid)
    assert split.status == "approved" and split.delete_source
    page = admin.get(f"/admin/sources/{sid}/split")
    assert "Splitting is queued" in page.text
    assert "Cutting" in _body(page.text) and "data-split-progress" in page.text
    assert 'data-editable="false"' in page.text


@needs_ffmpeg
def test_approve_without_a_saved_plan_asks_to_save_first(admin, admin_env):
    sid, _ = _source(admin_env)
    r = admin.post(f"/admin/sources/{sid}/split/approve", data={})
    assert r.status_code == 422
    assert "Save a plan first" in r.text
    assert not jobs.has_pending_for(admin_env.conn, sid, [JobType.SPLIT])


@needs_ffmpeg
def test_approve_rejects_an_invalid_plan(admin, admin_env):
    sid, _ = _source(admin_env)
    segs = _plan(10)
    segs[0]["keep"] = segs[1]["keep"] = False  # nothing kept: fine as a draft, not to cut
    assert admin.put(f"/admin/api/splits/{sid}", json={"segments": segs}).status_code == 200
    r = admin.post(f"/admin/sources/{sid}/split/approve", data={})
    assert r.status_code == 422
    assert 'role="alert"' in r.text
    assert not jobs.has_pending_for(admin_env.conn, sid, [JobType.SPLIT])


@needs_ffmpeg
def test_discard_removes_the_draft_and_goes_to_the_episode(admin, admin_env):
    sid, eid = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(10)})
    r = admin.post(f"/admin/sources/{sid}/split/discard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/episodes/{eid}"
    assert not library.get_split(admin_env.conn, sid).stored
    assert "Split plan discarded" in admin.get(r.headers["location"]).text


@needs_ffmpeg
def test_discard_is_refused_while_cutting(admin, admin_env):
    sid, _ = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(10)})
    library.approve_split(admin_env.conn, sid, delete_source=False, now=NOW)
    assert admin.post(f"/admin/sources/{sid}/split/discard").status_code == 422
    assert library.get_split(admin_env.conn, sid).stored


@needs_ffmpeg
def test_failed_proposal_shows_its_error(admin, admin_env):
    sid, _ = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(10)})
    library.mark_split(admin_env.conn, sid, "failed", now=NOW, error="ffmpeg exploded")
    page = _body(admin.get(f"/admin/sources/{sid}/split").text)
    assert "ffmpeg exploded" in page
    assert 'data-editable="true"' in page and "data-split-discard" in page


@needs_ffmpeg
def test_done_proposal_links_the_episodes(admin, admin_env):
    sid, eid = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(10)})
    library.mark_split(admin_env.conn, sid, "done", now=NOW)
    page = _body(admin.get(f"/admin/sources/{sid}/split").text)
    assert "Split into 1 episode" in page
    assert f"/admin/episodes/{eid}" in page


# --------------------------------------------------------------------------- episode page


def test_episode_page_offers_the_split(admin, admin_env):
    sid, eid = _source(admin_env, file=False)
    admin_env.conn.execute("UPDATE source_video SET file_path = 'eps/long.mp4' WHERE id = ?", (sid,))
    page = _body(admin.get(f"/admin/episodes/{eid}").text)
    assert f'href="/admin/sources/{sid}/split"' in page and "Split into episodes" in page


def test_episode_page_has_no_split_link_without_file_or_for_plain_episodes(admin, admin_env):
    sid, eid = _source(admin_env, file=False)
    assert "/split" not in _body(admin.get(f"/admin/episodes/{eid}").text)
    assert "/split" not in _body(admin.get(f"/admin/episodes/{admin_env.ids.a1}").text)


def _make_parts(env, sid, *, file=True):
    conn = env.conn
    conn.execute("DELETE FROM episode WHERE source_video_id = ?", (sid,))
    if file:
        conn.execute("UPDATE source_video SET file_path = 'eps/long.mp4' WHERE id = ?", (sid,))
    ids = []
    for i, (a, b) in enumerate([(0, 8), (8, 20)]):
        eid = library.add_episode(conn, env.ids.alpha, f"Part {i}", f"eps/p{i}.mp4", now=NOW, source_video_id=sid,
                                  duration_s=b - a)
        conn.execute("UPDATE episode SET start_s = ?, end_s = ? WHERE id = ?", (a, b, eid))
        ids.append(eid)
    return ids


def test_episode_page_of_a_part_shows_its_place_and_split_again(admin, admin_env):
    sid, _ = _source(admin_env, file=False, title="Long video")
    ids = _make_parts(admin_env, sid)
    page = _body(admin.get(f"/admin/episodes/{ids[1]}").text)
    assert "Part 2 of 2 of Long video" in page
    assert "Split again" in page and f"/admin/sources/{sid}/split" in page
    # SB-6: no redownload buttons, and a note instead
    assert "SponsorBlock no longer re-checks it" in page
    assert "/redownload" not in page


def test_episode_page_of_a_part_whose_original_was_deleted(admin, admin_env):
    sid, _ = _source(admin_env, file=False)
    ids = _make_parts(admin_env, sid, file=False)
    page = _body(admin.get(f"/admin/episodes/{ids[0]}").text)
    assert "Part 1 of 2" in page
    assert "was deleted after splitting" in page
    assert "Split again" not in page


def test_redownload_of_a_split_source_is_refused(admin, admin_env):
    sid, _ = _source(admin_env, file=False)
    ids = _make_parts(admin_env, sid)
    r = admin.post(f"/admin/episodes/{ids[0]}/redownload", data={"sponsorblock": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert not jobs.has_pending_for(admin_env.conn, sid, [JobType.REDOWNLOAD])
    assert "split into episodes" in admin.get(r.headers["location"]).text


# --------------------------------------------------------------------------- library page, job labels


@needs_ffmpeg
def test_library_lists_splits_to_review(admin, admin_env):
    assert "Splits to review" not in admin.get("/admin/library").text
    sid, eid = _source(admin_env)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": _plan(5, 12)})
    page = _body(admin.get("/admin/library").text)
    assert "Splits to review" in page
    assert f"/admin/sources/{sid}/split" in page
    assert f"/admin/img/episode/{eid}.jpg" in page
    assert "3 parts" in page
    assert "draft" in page
    library.mark_split(admin_env.conn, sid, "done", now=NOW)
    assert "Splits to review" not in admin.get("/admin/library").text


def test_jobs_page_labels_split_jobs(admin, admin_env):
    sid, _ = _source(admin_env, file=False)
    jobs.enqueue(admin_env.conn, JobType.SPLIT, sid, now=NOW)
    jobs.enqueue(admin_env.conn, JobType.DETECT, sid, now=NOW)
    data = admin.get("/admin/api/jobs").json()
    assert {j["label"] for j in data} == {"Long video"}
    page = admin.get("/admin/jobs").text
    assert "Long video" in page
    jobs.enqueue(admin_env.conn, JobType.SPLIT, None, now=NOW)
    labels = {j["label"] for j in admin.get("/admin/api/jobs").json()}
    assert "Split into episodes" in labels


def test_dutch_labels(admin, admin_env):
    sid, eid = _source(admin_env, file=False)
    admin_env.conn.execute("UPDATE source_video SET file_path = 'eps/long.mp4' WHERE id = ?", (sid,))
    page = _body(admin.get(f"/admin/episodes/{eid}", headers={"Accept-Language": "nl"}).text)
    assert "Split into episodes" not in page


@needs_ffmpeg
def test_delete_source_is_unticked_even_after_an_earlier_approval(admin, admin_env):  # A-21
    sid, _ = _source(admin_env)
    library.save_split(admin_env.conn, sid, splitting.segments_from_cuts(DURATION, [10]), now=NOW)
    library.approve_split(admin_env.conn, sid, delete_source=True, now=NOW)
    library.mark_split(admin_env.conn, sid, "done", now=NOW)
    html = admin.get(f"/admin/sources/{sid}/split").text
    assert 'name="delete_source" value="1">' in html
