"""Smart splitting in the admin (v6, ES-3, ES-4, ES-7, ES-10): marking title cards, the detect button,
the show's splitting profile, the detected badges and A-22's "publish as one video"."""

from __future__ import annotations

import json
import re

import pytest

from tellybox import library, splitting
from tellybox.jobs import JobType
from tests.web.admin.test_split_pages import DURATION, _body, _source, needs_ffmpeg
from tests.web.conftest import NOW

REGION = [0.1, 0.2, 0.3, 0.4]


def _mark(admin, sid, region=REGION, at_s=3.0):
    return admin.post(f"/admin/api/splits/{sid}/reference", json={"at_s": at_s, "region": region})


def _state(admin, sid):
    return admin.get(f"/admin/api/splits/{sid}").json()


def _profile_form(**over):
    form = {"threshold": "6", "length_hint": "", "snap_window": "30"}
    form.update(over)
    return {k: v for k, v in form.items() if v is not None}


# --------------------------------------------------------------------------- marking a title card (ES-3)


@needs_ffmpeg
def test_mark_stores_the_frame_region_and_hash(admin, admin_env):
    sid, _ = _source(admin_env)
    r = _mark(admin, sid)
    assert r.status_code == 200, r.text
    profile = r.json()
    assert profile["usable"] and profile["auto_detect"] is False and profile["length_hint_s"] is None
    [ref] = profile["references"]
    assert ref["region"] == REGION and ref["image_url"] == f"/admin/img/split-reference/{ref['id']}.jpg"
    row = admin_env.conn.execute("SELECT * FROM split_reference").fetchone()
    assert (row["show_id"], row["source_video_id"], row["at_s"]) == (admin_env.ids.alpha, sid, 3.0)
    assert json.loads(row["region_json"]) == REGION and len(row["card_hash"]) == 16
    assert _state(admin, sid)["profile"] == profile


@needs_ffmpeg
def test_mark_the_whole_frame(admin, admin_env):
    sid, _ = _source(admin_env)
    [ref] = _mark(admin, sid, region=None).json()["references"]
    assert ref["region"] is None


@needs_ffmpeg
def test_reference_image_is_served_with_revalidation(admin, admin_env):
    sid, _ = _source(admin_env)
    ref = _mark(admin, sid).json()["references"][0]
    r = admin.get(ref["image_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-cache"
    assert r.content[:2] == b"\xff\xd8"
    assert admin.get("/admin/img/split-reference/99999.jpg").status_code == 404


@needs_ffmpeg
def test_mark_rejects_bad_input(admin, admin_env):
    sid, _ = _source(admin_env)
    for body in ({"at_s": 3, "region": [0.9, 0.9, 0.5, 0.5]}, {"at_s": 3, "region": [0.1, 0.1]},
                 {"at_s": 3, "region": "all"}, {"region": None}, {"at_s": "soon"}, {"at_s": -1, "region": None}):
        assert admin.post(f"/admin/api/splits/{sid}/reference", json=body).status_code == 422, body
    assert admin.post(f"/admin/api/splits/{sid}/reference", content=b"nope").status_code == 422
    assert admin_env.conn.execute("SELECT COUNT(*) FROM split_reference").fetchone()[0] == 0


@needs_ffmpeg
def test_mark_conflicts_without_the_file_and_404s_for_an_unknown_source(admin, admin_env):
    sid, _ = _source(admin_env)
    admin_env.conn.execute("UPDATE source_video SET file_path = NULL WHERE id = ?", (sid,))
    r = _mark(admin, sid)
    assert r.status_code == 409 and r.json()["error"]
    assert _mark(admin, 99999).status_code == 404


def test_mark_needs_a_session_and_the_origin(anon, admin, admin_env):
    assert anon.post("/admin/api/splits/1/reference", json={"at_s": 1, "region": None},
                     headers={"accept": "application/json"}).status_code == 401
    r = admin.post("/admin/api/splits/1/reference", json={"at_s": 1, "region": None},
                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- Find cuts (ES-4)


@needs_ffmpeg
def test_detect_needs_a_marked_title_card(admin, admin_env):
    sid, _ = _source(admin_env)
    r = admin.post(f"/admin/api/splits/{sid}/detect")
    assert r.status_code == 409 and "title card" in r.json()["error"]
    assert admin_env.conn.execute("SELECT COUNT(*) FROM job WHERE type = 'detect'").fetchone()[0] == 0


@needs_ffmpeg
def test_detect_queues_a_job_and_the_state_shows_it(admin, admin_env):
    sid, _ = _source(admin_env)
    assert _state(admin, sid)["detect_job"] is None
    _mark(admin, sid)
    r = admin.post(f"/admin/api/splits/{sid}/detect")
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    row = admin_env.conn.execute("SELECT type, target_id, status FROM job WHERE id = ?", (job_id,)).fetchone()
    assert (row["type"], row["target_id"], row["status"]) == (JobType.DETECT.value, sid, "queued")
    job = _state(admin, sid)["detect_job"]
    assert (job["id"], job["status"]) == (job_id, "queued")
    again = admin.post(f"/admin/api/splits/{sid}/detect")  # one at a time
    assert again.status_code == 409 and again.json()["error"]


@needs_ffmpeg
def test_detect_conflicts_while_cutting_and_without_the_file(admin, admin_env):
    sid, _ = _source(admin_env)
    _mark(admin, sid)
    admin.put(f"/admin/api/splits/{sid}", json={"segments": [
        {"start_s": 0, "end_s": 10, "title": "", "keep": True}, {"start_s": 10, "end_s": DURATION, "title": "", "keep": True}]})
    library.approve_split(admin_env.conn, sid, delete_source=False, now=NOW)
    assert admin.post(f"/admin/api/splits/{sid}/detect").status_code == 409
    sid2, _ = _source(admin_env, file=False, ytid="other")
    assert admin.post(f"/admin/api/splits/{sid2}/detect").status_code == 409
    assert admin.post("/admin/api/splits/99999/detect").status_code == 404


# --------------------------------------------------------------------------- state additions


@needs_ffmpeg
def test_state_without_a_profile_or_detection(admin, admin_env):
    sid, _ = _source(admin_env)
    state = _state(admin, sid)
    assert state["show_id"] == admin_env.ids.alpha
    assert state["profile"] == {"usable": False, "references": [], "length_hint_s": None, "auto_detect": False}
    assert state["detected"] is None and state["awaiting_split"] is False and state["detect_job"] is None


@needs_ffmpeg
def test_state_carries_the_detected_cuts_and_the_hold(admin, admin_env):
    sid, _ = _source(admin_env)
    conn = admin_env.conn
    conn.execute("UPDATE source_video SET awaiting_split = 1 WHERE id = ?", (sid,))
    segments = splitting.segments_from_cuts(DURATION, [8.0], ["", "Two"])
    detected = [{"at_s": 8.0, "title_hit_s": 8.5, "confidence": 0.9, "snapped": "black", "title": "Two"}]
    library.save_detected_split(conn, sid, segments, detected, now=NOW)
    state = _state(admin, sid)
    assert state["detected"] == detected and state["awaiting_split"] is True
    assert (state["status"], state["origin"]) == ("review", "detected")
    # once the admin edits the plan it is no longer "the detected one"
    admin.put(f"/admin/api/splits/{sid}", json={"segments": [
        {"start_s": s.start_s, "end_s": s.end_s, "title": s.title, "keep": s.keep} for s in segments], "origin": "manual"})
    assert _state(admin, sid)["detected"] is None


# --------------------------------------------------------------------------- the split page


@needs_ffmpeg
def test_split_page_has_the_marking_and_detection_hooks(admin, admin_env):
    sid, _ = _source(admin_env)
    html = admin.get(f"/admin/sources/{sid}/split").text
    page = _body(html)
    for hook in ('data-action="mark"', "data-mark-canvas", "data-mark-bar", 'data-mark="whole"', 'data-mark="save"',
                 'data-mark="cancel"', "data-ref-list", 'data-action="detect"', "data-detect-progress", "data-detect-status"):
        assert hook in page, hook
    assert f'href="/admin/shows/{admin_env.ids.alpha}#splitting"' in page
    assert "/admin/static/split_mark.js" not in page  # imported by split.js
    state = json.loads(re.search(r'<script type="application/json" id="split-state">(.*?)</script>', html, re.S).group(1))
    assert state["profile"]["usable"] is False and state["awaiting_split"] is False
    assert "data-awaiting" not in page


@needs_ffmpeg
def test_split_page_says_when_the_video_is_held_for_its_split(admin, admin_env):
    sid, _ = _source(admin_env)
    admin_env.conn.execute("UPDATE source_video SET awaiting_split = 1 WHERE id = ?", (sid,))
    page = _body(admin.get(f"/admin/sources/{sid}/split").text)
    assert "data-awaiting" in page and "Hidden from the kids until you approve the split" in page
    assert f'action="/admin/sources/{sid}/publish-whole"' in page and "Publish as one video" in page


# --------------------------------------------------------------------------- publish as one video (A-22)


@needs_ffmpeg
def test_publish_whole_unhides_the_episode_and_clears_the_hold(admin, admin_env):
    sid, eid = _source(admin_env)
    conn = admin_env.conn
    conn.execute("UPDATE source_video SET awaiting_split = 1 WHERE id = ?", (sid,))
    conn.execute("UPDATE episode SET hidden = 1 WHERE id = ?", (eid,))
    r = admin.post(f"/admin/sources/{sid}/publish-whole", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/episodes/{eid}"
    assert conn.execute("SELECT awaiting_split FROM source_video WHERE id = ?", (sid,)).fetchone()[0] == 0
    assert conn.execute("SELECT hidden FROM episode WHERE id = ?", (eid,)).fetchone()[0] == 0
    assert admin.post(f"/admin/sources/{sid}/publish-whole").status_code == 404  # nothing is held any more
    assert admin.post("/admin/sources/99999/publish-whole").status_code == 404


# --------------------------------------------------------------------------- the show's profile


def test_show_page_card_without_title_cards(admin, admin_env):
    page = _body(admin.get(f"/admin/shows/{admin_env.ids.alpha}").text)
    assert 'id="splitting"' in page and "No title cards marked yet" in page
    assert f'action="/admin/shows/{admin_env.ids.alpha}/split-profile"' in page
    assert 'name="threshold"' in page and 'value="6"' in page and 'value="30"' in page
    assert "Lower is stricter" in page and "stay hidden until you approve them" in page
    assert "ref-thumb" not in page


@needs_ffmpeg
def test_show_page_lists_the_references_with_their_region_and_delete(admin, admin_env):
    sid, _ = _source(admin_env)
    _mark(admin, sid)
    _mark(admin, sid, region=None, at_s=5.0)
    page = _body(admin.get(f"/admin/shows/{admin_env.ids.alpha}").text)
    assert page.count('class="ref-thumb"') == 2 and page.count('class="ref-region"') == 1
    assert "left:10.0%;top:20.0%" in page
    assert "No title cards marked yet" not in page
    ids = [r["id"] for r in _state(admin, sid)["profile"]["references"]]
    for rid in ids:
        assert f'action="/admin/split-references/{rid}/delete"' in page


@needs_ffmpeg
def test_delete_a_reference_removes_the_file(admin, admin_env):
    sid, _ = _source(admin_env)
    ref = _mark(admin, sid).json()["references"][0]
    rel = admin_env.conn.execute("SELECT image_path FROM split_reference").fetchone()[0]
    assert (admin_env.config.media_dir / rel).is_file()
    r = admin.post(f"/admin/split-references/{ref['id']}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/shows/{admin_env.ids.alpha}#splitting"
    assert admin_env.conn.execute("SELECT COUNT(*) FROM split_reference").fetchone()[0] == 0
    assert not (admin_env.config.media_dir / rel).exists()
    assert admin.post(f"/admin/split-references/{ref['id']}/delete").status_code == 404
    assert _state(admin, sid)["profile"]["usable"] is False


def test_save_the_profile(admin, admin_env):
    show = admin_env.ids.alpha
    r = admin.post(f"/admin/shows/{show}/split-profile", follow_redirects=False, data=_profile_form(
        threshold="9", length_hint="11:30", snap_window="12.5", ocr="1", auto_detect="1",
        ocr_x="10", ocr_y="70", ocr_w="80", ocr_h="20"))
    assert r.status_code == 303 and r.headers["location"] == f"/admin/shows/{show}#splitting"
    p = library.get_split_profile(admin_env.conn, show)
    assert (p.match_threshold, p.length_hint_s, p.snap_window_s, p.ocr, p.auto_detect) == (9, 690.0, 12.5, True, True)
    assert p.ocr_region == [0.1, 0.7, 0.8, 0.2]
    page = _body(admin.get(f"/admin/shows/{show}").text)
    assert 'value="9"' in page and 'value="11:30"' in page and 'value="12.5"' in page and 'value="70"' in page
    assert re.search(r'name="ocr"[^>]*checked', page) and re.search(r'name="auto_detect"[^>]*checked', page)


def test_a_plain_length_hint_is_minutes_and_blank_clears_it(admin, admin_env):
    show = admin_env.ids.alpha
    admin.post(f"/admin/shows/{show}/split-profile", data=_profile_form(length_hint="11"))
    assert library.get_split_profile(admin_env.conn, show).length_hint_s == 660.0
    assert 'value="11"' in _body(admin.get(f"/admin/shows/{show}").text)
    admin.post(f"/admin/shows/{show}/split-profile", data=_profile_form(length_hint=""))
    p = library.get_split_profile(admin_env.conn, show)
    assert p.length_hint_s is None and p.ocr is False and p.auto_detect is False and p.ocr_region is None


@pytest.mark.parametrize("field, value", [
    ("threshold", "strict"), ("threshold", "40"), ("threshold", "-1"), ("length_hint", "soon"), ("length_hint", "0:10"),
    ("length_hint", "999"), ("snap_window", "x"), ("snap_window", "500"), ("ocr_x", "10"),  # a partial region
])
def test_profile_errors_rerender_with_422_and_save_nothing(admin, admin_env, field, value):
    show = admin_env.ids.alpha
    r = admin.post(f"/admin/shows/{show}/split-profile", data=_profile_form(**{field: value}))
    assert r.status_code == 422
    assert 'class="flash error"' in r.text
    assert library.get_split_profile(admin_env.conn, show).stored is False
    if field != "ocr_x":
        assert f'value="{value}"' in _body(r.text)  # what was typed stays


def test_profile_region_must_fit_the_frame(admin, admin_env):
    r = admin.post(f"/admin/shows/{admin_env.ids.alpha}/split-profile",
                   data=_profile_form(ocr_x="60", ocr_y="0", ocr_w="60", ocr_h="10"))
    assert r.status_code == 422 and "four percentages" in r.text
    assert admin.post("/admin/shows/99999/split-profile", data=_profile_form()).status_code == 404


# --------------------------------------------------------------------------- the library


@needs_ffmpeg
def test_library_badges_for_detected_and_held_proposals(admin, admin_env):
    sid, _ = _source(admin_env)
    page = _body(admin.get("/admin/library").text)
    assert "Splits to review" not in page
    admin.put(f"/admin/api/splits/{sid}", json={"segments": [
        {"start_s": 0, "end_s": 8, "title": "", "keep": True}, {"start_s": 8, "end_s": DURATION, "title": "", "keep": True}]})
    page = _body(admin.get("/admin/library").text)
    assert "Splits to review" in page and "Detected" not in page and "Hidden until approved" not in page
    segments = splitting.segments_from_cuts(DURATION, [8.0], [])
    library.save_detected_split(admin_env.conn, sid, segments, [], now=NOW)
    page = _body(admin.get("/admin/library").text)
    assert "Detected" in page and "Hidden until approved" not in page
    admin_env.conn.execute("UPDATE source_video SET awaiting_split = 1 WHERE id = ?", (sid,))
    assert "Hidden until approved" in _body(admin.get("/admin/library").text)
