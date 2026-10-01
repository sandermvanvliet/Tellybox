"""The detect job and ES-10 auto-detection in the worker (ES-4, ES-7, ES-9, ES-10, A-22).

The engine is replaced by known cuts here; one test runs the real engine and skips until it exists.
"""

from __future__ import annotations

import pytest

from tellybox import detect, ingest, jobs, library, media_format
from tellybox.detect import Cut
from tellybox.jobs import JobStatus, JobType
from tellybox.splitting import Segment

from detect_clips import LOGO_REGION, make_compilation
from test_ingest import (  # noqa: F401
    FakeYtDlp, clock, conn, info, media, run_next, runner, source,
)


@pytest.fixture(scope="session")
def comp(tmp_path_factory):
    return make_compilation(tmp_path_factory.mktemp("detectjob") / "comp.mp4", [12, 15, 10], lead_s=6)


@pytest.fixture
def fake(comp):
    return FakeYtDlp(comp.path)


def cuts_of(comp):
    return [Cut(at_s=b, title_hit_s=c, confidence=0.9, snapped="black", title=t)
            for b, c, t in zip(comp.black_starts, comp.card_starts, comp.titles)]


@pytest.fixture
def engine(monkeypatch, comp):
    """detect.detect returns the compilation's real cut points; the calls are recorded."""
    calls = []

    def fake_detect(video, profile, *, on_progress=None):
        calls.append((video, profile))
        if on_progress:
            on_progress(0.5)
        return list(reversed(cuts_of(comp)))  # unordered on purpose

    monkeypatch.setattr(detect, "detect", fake_detect)
    return calls


def make_show(runner, clock, comp, *, auto=False, hint=None, ocr=False):
    """The test channel's show, with one marked title card."""
    conn = runner.conn
    found = library.find_show_by_channel(conn, "UCkids")
    show = found.id if found else library.create_show(
        conn, "Kids Channel", now=clock.now(), youtube_channel_id="UCkids")
    library.add_split_reference(conn, runner.media_dir, show, comp.reference, LOGO_REGION,
                                source_video_id=None, at_s=None, now=clock.now())
    library.save_split_profile(conn, show, match_threshold=6, length_hint_s=hint, snap_window_s=30, ocr=ocr,
                               ocr_region=None, auto_detect=auto, now=clock.now())
    return show


def download(conn, clock, runner, *, publish=True):
    sid, _ = ingest.add(conn, info(), publish=publish, now=clock.now())
    assert run_next(runner, clock).status == JobStatus.READY
    return sid


def detect_job(conn, clock, runner, sid):
    library.request_detect(conn, sid, now=clock.now())
    return run_next(runner, clock)


def proposal(conn, sid):
    return library.get_split(conn, sid)


def episodes(conn, sid):
    return library.list_source_episodes(conn, sid)


def detect_jobs(conn):
    return [j for j in jobs.list_jobs(conn) if j.type == JobType.DETECT]


# --------------------------------------------------------------------------- the detect job


def test_detect_job_saves_a_proposal_to_review(conn, clock, runner, comp, engine):  # ES-4, ES-7
    make_show(runner, clock, comp, hint=600)
    sid = download(conn, clock, runner)
    job = detect_job(conn, clock, runner, sid)
    assert job.status == JobStatus.READY
    p = proposal(conn, sid)
    assert (p.status, p.origin, p.stored) == ("review", "detected", True)
    assert [round(s.start_s, 1) for s in p.segments[1:]] == [round(b, 1) for b in comp.black_starts]
    assert p.segments[0].start_s == 0 and p.segments[-1].end_s == pytest.approx(p.duration_s)
    assert [s.title for s in p.segments] == ["", *comp.titles]  # the OCR title names the part after its cut
    detected = library.get_detected(conn, sid)
    assert [d["at_s"] for d in detected] == comp.black_starts
    assert detected[0] == {"at_s": comp.black_starts[0], "title_hit_s": comp.card_starts[0],
                           "confidence": 0.9, "snapped": "black", "title": "Episode 1"}
    assert all(not e.hidden for e in episodes(conn, sid))  # detection never touches the episodes


def test_detect_job_builds_the_profile_from_the_show(conn, clock, runner, comp, engine):
    show = make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    library.save_split_profile(conn, show, match_threshold=9, length_hint_s=600, snap_window_s=12, ocr=True,
                               ocr_region=[0.1, 0.5, 0.8, 0.3], auto_detect=False, now=clock.now())
    detect_job(conn, clock, runner, sid)
    ((video, profile),) = engine
    assert video == runner.media_dir / source(conn, sid)["file_path"]
    ref = library.get_split_profile(conn, show).references[0]
    assert list(profile.references) == [detect.Reference(ref.card_hash, detect.Region(*LOGO_REGION))]
    assert (profile.match_threshold, profile.length_hint_s, profile.snap_window_s, profile.ocr) == (9, 600, 12, True)
    assert profile.ocr_region == detect.Region(0.1, 0.5, 0.8, 0.3)


def test_detect_job_without_a_reference_fails(conn, clock, runner, engine):  # ES-3
    sid = download(conn, clock, runner)
    jobs.enqueue(conn, JobType.DETECT, sid, now=clock.now(), max_attempts=2)
    job = run_next(runner, clock)
    assert job.status == JobStatus.FAILED and "no title card marked" in job.error
    assert not engine and not proposal(conn, sid).stored


def test_detect_job_with_no_cuts_still_proposes_the_whole_video(conn, clock, runner, comp, monkeypatch):
    monkeypatch.setattr(detect, "detect", lambda *a, **k: [])
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    assert detect_job(conn, clock, runner, sid).status == JobStatus.READY
    p = proposal(conn, sid)
    assert (p.status, p.origin) == ("review", "detected") and len(p.segments) == 1
    assert library.get_detected(conn, sid) == []


def test_detect_job_drops_cuts_that_leave_a_tiny_part(conn, clock, runner, comp, monkeypatch):
    cuts = [Cut(at_s=a, title_hit_s=a, confidence=0.8, title=f"t{a}") for a in (10.0, 12.0, 30.0, comp.duration_s - 2)]
    monkeypatch.setattr(detect, "detect", lambda *a, **k: cuts)
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    detect_job(conn, clock, runner, sid)
    p = proposal(conn, sid)
    assert [s.start_s for s in p.segments] == [0, 10.0, 30.0]  # 12 is 2 s after 10; the last is 2 s from the end
    assert [s.title for s in p.segments] == ["", "t10.0", "t30.0"]
    assert [x["at_s"] for x in library.get_detected(conn, sid)] == [10.0, 30.0]


def test_detect_job_after_the_plan_was_approved_drops_the_result(conn, clock, runner, comp, engine):
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    library.request_detect(conn, sid, now=clock.now())
    claimed = jobs.claim_next(conn, now=clock.now())
    mine = [Segment(0, 20, "Mine"), Segment(20, comp.duration_s, "")]
    library.save_split(conn, sid, mine, now=clock.now())  # the admin approves while detection runs
    library.approve_split(conn, sid, delete_source=False, now=clock.now())
    runner.run(claimed)
    assert jobs.get(conn, claimed.id).status == JobStatus.READY
    p = proposal(conn, sid)
    assert (p.status, p.origin, [s.title for s in p.segments]) == ("approved", "manual", ["Mine", ""])
    assert library.get_detected(conn, sid) is None


def test_detect_job_replaces_an_unapproved_draft(conn, clock, runner, comp, engine):
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    library.save_split(conn, sid, [Segment(0, 20, "Mine"), Segment(20, comp.duration_s, "")], now=clock.now())
    detect_job(conn, clock, runner, sid)
    assert proposal(conn, sid).origin == "detected"


def test_a_media_error_is_retried_once(conn, clock, runner, comp, monkeypatch):
    def boom(*a, **k):
        raise media_format.MediaError("ffmpeg died")

    monkeypatch.setattr(detect, "detect", boom)
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    library.request_detect(conn, sid, now=clock.now())
    job = run_next(runner, clock)
    assert job.status == JobStatus.QUEUED and "ffmpeg died" in job.error  # max_attempts is 2
    clock.advance(hours=1)  # the retry backoff
    assert run_next(runner, clock).status == JobStatus.FAILED
    assert not proposal(conn, sid).stored


def test_detect_job_with_the_file_gone_fails(conn, clock, runner, comp, engine, media):
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    library.request_detect(conn, sid, now=clock.now())
    (media / source(conn, sid)["file_path"]).unlink()
    job = run_next(runner, clock)
    assert job.status == JobStatus.FAILED and "gone" in job.error


def test_detect_does_not_wait_for_playback(conn, clock, runner, comp, engine):
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    runner.now_playing = lambda: episodes(conn, sid)[0].id
    assert detect_job(conn, clock, runner, sid).status == JobStatus.READY


def test_real_engine_end_to_end(conn, clock, runner, comp):  # skipped while detect.detect is the stub
    make_show(runner, clock, comp)
    sid = download(conn, clock, runner)
    library.request_detect(conn, sid, now=clock.now())
    job = run_next(runner, clock)
    if "NotImplementedError" in (job.error or ""):
        pytest.skip("detect.detect is a stub")
    assert job.status == JobStatus.READY, job.error
    starts = [s.start_s for s in proposal(conn, sid).segments[1:]]
    assert len(starts) == len(comp.black_starts)
    assert all(abs(a - b) < 0.2 for a, b in zip(starts, comp.black_starts))


# --------------------------------------------------------------------------- ES-10: auto-detect after a download


def test_auto_detect_holds_the_new_compilation_and_queues_detection(conn, clock, runner, comp):  # ES-10, A-22
    show = make_show(runner, clock, comp, auto=True, hint=30)  # 1.5 x 30 s is less than the compilation
    sid = download(conn, clock, runner)
    (ep,) = episodes(conn, sid)
    assert ep.show_id == show and ep.hidden
    assert source(conn, sid)["awaiting_split"] == 1
    assert [(j.target_id, j.status) for j in detect_jobs(conn)] == [(sid, JobStatus.QUEUED)]


def test_auto_detect_without_a_hint_needs_twenty_minutes(conn, clock, runner, comp, monkeypatch):
    monkeypatch.setattr(ingest, "AUTO_DETECT_NO_HINT_S", 30)
    make_show(runner, clock, comp, auto=True)
    sid = download(conn, clock, runner)
    assert source(conn, sid)["awaiting_split"] == 1 and len(detect_jobs(conn)) == 1


@pytest.mark.parametrize("case", ["short", "auto off", "no hint, short", "no title card"])
def test_no_auto_detect(conn, clock, runner, comp, case):
    if case == "no title card":
        show = library.create_show(conn, "Kids Channel", now=clock.now(), youtube_channel_id="UCkids")
        library.save_split_profile(conn, show, match_threshold=6, length_hint_s=30, snap_window_s=30, ocr=False,
                                   ocr_region=None, auto_detect=True, now=clock.now())
    else:
        make_show(runner, clock, comp, auto=case != "auto off",
                  hint=None if case == "no hint, short" else 300 if case == "short" else 30)
    sid = download(conn, clock, runner)
    (ep,) = episodes(conn, sid)
    assert not ep.hidden and source(conn, sid)["awaiting_split"] == 0
    assert detect_jobs(conn) == []


def test_auto_detect_does_not_apply_to_a_redownload(conn, clock, runner, comp):
    sid = download(conn, clock, runner)
    make_show(runner, clock, comp, auto=True, hint=30)
    jobs.enqueue(conn, JobType.REDOWNLOAD, sid, now=clock.now())
    assert run_next(runner, clock).status == JobStatus.READY
    assert not episodes(conn, sid)[0].hidden and source(conn, sid)["awaiting_split"] == 0
    assert detect_jobs(conn) == []


def test_a_held_compilation_is_also_awaiting(conn, clock, runner, comp):
    make_show(runner, clock, comp, auto=True, hint=30)
    sid = download(conn, clock, runner, publish=False)
    assert source(conn, sid)["awaiting_split"] == 1 and episodes(conn, sid)[0].hidden
    assert len(detect_jobs(conn)) == 1


# --------------------------------------------------------------------------- A-22: approving the split shows the parts


def approve_detected(conn, clock, runner, sid):
    assert run_next(runner, clock).type == JobType.DETECT  # the detection ES-10 queued
    assert proposal(conn, sid).origin == "detected"
    library.approve_split(conn, sid, delete_source=False, now=clock.now())
    return run_next(runner, clock)


def test_approving_the_auto_proposal_shows_the_parts(conn, clock, runner, comp, engine):  # A-22
    make_show(runner, clock, comp, auto=True, hint=30)
    sid = download(conn, clock, runner)
    assert episodes(conn, sid)[0].hidden
    job = approve_detected(conn, clock, runner, sid)
    assert job.status == JobStatus.READY, job.error
    parts = episodes(conn, sid)
    assert len(parts) == len(comp.black_starts) + 1
    assert all(not e.hidden for e in parts)
    assert source(conn, sid)["awaiting_split"] == 0


def test_a_held_source_keeps_its_parts_hidden(conn, clock, runner, comp, engine):  # A-22, LM-3
    make_show(runner, clock, comp, auto=True, hint=30)
    sid = download(conn, clock, runner, publish=False)
    job = approve_detected(conn, clock, runner, sid)
    assert job.status == JobStatus.READY, job.error
    assert all(e.hidden for e in episodes(conn, sid))
    assert source(conn, sid)["awaiting_split"] == 0
