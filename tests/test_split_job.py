"""The split job: cutting an approved plan into episodes (ES-8, SB-6). Real ffmpeg on small lavfi clips."""

from datetime import timedelta
from pathlib import Path

import pytest

from tellybox import ingest, jobs, library, media_format, splitting
from tellybox.jobs import JobStatus, JobType
from tellybox.splitting import Segment

# Fixtures (clock, conn, media, fake, runner ...) and helpers come from the ingest tests.
from test_ingest import (  # noqa: F401
    FakeYtDlp, _ffmpeg, clock, conn, info, media, run_next, runner, source,
)

URL = "https://www.youtube.com/watch?v=abc123"
LONG_S = 20


@pytest.fixture(scope="session")
def long_clip(tmp_path_factory) -> Path:
    return _ffmpeg(tmp_path_factory.mktemp("clips") / "long.mp4",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", seconds=LONG_S)


@pytest.fixture(scope="session")
def shorter_clip(tmp_path_factory) -> Path:
    return _ffmpeg(tmp_path_factory.mktemp("clips") / "shorter.mp4",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", seconds=8)


@pytest.fixture
def fake(long_clip):  # noqa: F811
    return FakeYtDlp(long_clip)


PLAN = [
    Segment(0, 7, "One"),
    Segment(7, 13, "Left out", keep=False),
    Segment(13, 20.0, ""),
]


def published(conn, clock, runner, *, publish=True):
    sid, _ = ingest.add(conn, info(), publish=publish, now=clock.now())
    assert run_next(runner, clock).status == JobStatus.READY
    return sid


def approve(conn, clock, sid, segments=PLAN, *, delete_source=False):
    library.save_split(conn, sid, segments, now=clock.now())
    return library.approve_split(conn, sid, delete_source=delete_source, now=clock.now())


def split(conn, clock, runner, sid, segments=PLAN, *, delete_source=False):
    approve(conn, clock, sid, segments, delete_source=delete_source)
    return run_next(runner, clock)


def parts(conn, sid):
    return library.list_source_episodes(conn, sid)


def proposal_status(conn, sid):
    return conn.execute("SELECT status, error FROM split_proposal WHERE source_video_id = ?", (sid,)).fetchone()


def test_split_cuts_the_kept_parts(conn, clock, runner, media):  # ES-8
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    job = split(conn, clock, runner, sid)
    assert job.status == JobStatus.READY
    eps = parts(conn, sid)
    assert [e.title for e in eps] == ["One", "Episode one (2)"]  # own title, then the fallback by kept number
    assert [(e.start_s, e.end_s) for e in eps] == [(0, 7), (13, 20)]  # start_s 0 is the SB-6 marker too
    assert [round(e.duration_s) for e in eps] == [7, 7]
    for e, want in zip(eps, (7, 7)):
        path = media / e.file_path
        assert path.name.startswith(f"abc123-s{job.id}-")
        assert media_format.verify(path).duration_s == pytest.approx(want, abs=0.1)
        assert e.thumbnail_path and (media / e.thumbnail_path).stat().st_size > 0
        assert not e.hidden
    assert library.get_episode(conn, old.id) is None
    assert proposal_status(conn, sid)["status"] == "done"
    assert library.is_split(conn, sid)
    assert not (media / ".tmp" / f"job-{job.id}").exists()


def test_split_keeps_the_source_file_by_default(conn, clock, runner, media):
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    split(conn, clock, runner, sid)
    assert (media / source(conn, sid)["file_path"]).is_file()  # the old episode shared it
    assert source(conn, sid)["file_path"] == old.file_path


def test_split_with_delete_source(conn, clock, runner, media):  # A-21
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    src_thumb = source(conn, sid)["thumbnail_path"]
    split(conn, clock, runner, sid, delete_source=True)
    row = source(conn, sid)
    assert row["file_path"] is None
    assert not (media / old.file_path).exists()
    assert row["thumbnail_path"] == src_thumb and (media / src_thumb).is_file()  # still referenced
    assert len(parts(conn, sid)) == 2
    with pytest.raises(library.SourceGone):  # it can't be split again
        library.save_split(conn, sid, PLAN, now=clock.now())


def test_split_takes_the_old_episodes_place_in_the_show(conn, clock, runner):
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    show = old.show_id
    first = library.add_episode(conn, show, "First", "a.mp4", now=clock.now(), sort_order=0)
    last = library.add_episode(conn, show, "Last", "b.mp4", now=clock.now(), sort_order=5)
    conn.execute("UPDATE episode SET sort_order = 3 WHERE id = ?", (old.id,))
    split(conn, clock, runner, sid)
    eps = library.list_episodes(conn, show, include_hidden=True)
    assert [e.title for e in eps] == ["First", "One", "Episode one (2)", "Last"]
    assert [e.sort_order for e in eps] == [0, 1, 2, 3]
    assert (eps[0].id, eps[3].id) == (first, last)


def test_split_removes_positions_of_the_old_episode(conn, clock, runner):
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    profile = conn.execute("SELECT id FROM profile LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO playback_position (profile_id, episode_id, position_s, updated_at) VALUES (?, ?, 5, 'x')", (profile, old.id))
    split(conn, clock, runner, sid)
    assert conn.execute("SELECT count(*) FROM playback_position WHERE episode_id = ?", (old.id,)).fetchone()[0] == 0


def test_split_of_a_held_source_gives_hidden_parts(conn, clock, runner):
    sid = published(conn, clock, runner, publish=False)
    split(conn, clock, runner, sid)
    assert all(e.hidden for e in parts(conn, sid))


def test_split_of_a_hidden_episode_gives_hidden_parts(conn, clock, runner):
    sid = published(conn, clock, runner)
    conn.execute("UPDATE episode SET hidden = 1 WHERE source_video_id = ?", (sid,))
    split(conn, clock, runner, sid)
    assert all(e.hidden for e in parts(conn, sid))


def test_resplit_replaces_the_earlier_parts(conn, clock, runner, media):
    sid = published(conn, clock, runner)
    split(conn, clock, runner, sid)
    first = parts(conn, sid)
    files = [media / e.file_path for e in first] + [media / e.thumbnail_path for e in first]
    again = [Segment(0, 10, "A"), Segment(10, 20.0, "B")]
    job = split(conn, clock, runner, sid, again)
    assert job.status == JobStatus.READY
    eps = parts(conn, sid)
    assert [e.title for e in eps] == ["A", "B"] and not {e.id for e in eps} & {e.id for e in first}
    assert not any(f.exists() for f in files)
    assert all((media / e.file_path).is_file() for e in eps)
    assert (media / source(conn, sid)["file_path"]).is_file()


def test_progress_is_weighted_by_part_length(conn, clock, runner, monkeypatch):
    sid = published(conn, clock, runner)
    seen = []
    monkeypatch.setattr(jobs, "heartbeat", lambda conn, job_id, *, now, progress=None: seen.append(progress))
    split(conn, clock, runner, sid)
    assert seen and seen == sorted(seen) and 0 < seen[0] and seen[-1] == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("state", ["playing", "unreachable"])
def test_split_waits_while_a_part_is_on_the_tv(conn, clock, runner, state):
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    if state == "playing":
        runner.now_playing = lambda: old.id
    else:
        def down():
            raise ConnectionError("cast service down")
        runner.now_playing = down
    job = split(conn, clock, runner, sid)
    assert job.status == JobStatus.QUEUED and job.attempts == 0 and job.error.startswith("waiting")
    assert job.run_after == clock.now() + timedelta(minutes=20)
    assert proposal_status(conn, sid)["status"] == "approved"
    assert [e.id for e in parts(conn, sid)] == [old.id]

    runner.now_playing = lambda: None
    clock.advance(minutes=20)
    assert run_next(runner, clock).status == JobStatus.READY
    assert len(parts(conn, sid)) == 2


def test_split_waits_when_a_part_starts_during_the_cut(conn, clock, runner, media):
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    calls = iter([None, old.id])
    runner.now_playing = lambda: next(calls)
    job = split(conn, clock, runner, sid)
    assert job.status == JobStatus.QUEUED and job.error.startswith("waiting")
    assert proposal_status(conn, sid)["status"] == "approved"
    assert [e.id for e in parts(conn, sid)] == [old.id]
    assert not (media / ".tmp" / f"job-{job.id}").exists()


def test_cut_failure_changes_nothing_and_retries(conn, clock, runner, media, monkeypatch):  # NF-8
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    real, calls = media_format.cut, []

    def flaky(src, dst, start_s, end_s, **kw):
        calls.append(start_s)
        if len(calls) == 2:
            raise media_format.MediaError("boom")
        return real(src, dst, start_s, end_s, **kw)

    monkeypatch.setattr(media_format, "cut", flaky)
    before = sorted(p.name for p in (media / "shows").rglob("*") if p.is_file())
    job = split(conn, clock, runner, sid)
    assert job.status == JobStatus.QUEUED and "boom" in job.error  # one attempt left
    assert proposal_status(conn, sid)["status"] == "approved"
    assert [e.id for e in parts(conn, sid)] == [old.id]
    assert sorted(p.name for p in (media / "shows").rglob("*") if p.is_file()) == before
    assert not (media / "thumbs").exists() and not (media / ".tmp" / f"job-{job.id}").exists()

    calls.clear()
    clock.advance(hours=2)
    job = run_next(runner, clock)  # fails again: the last attempt
    assert job.status == JobStatus.FAILED
    row = proposal_status(conn, sid)
    assert row["status"] == "failed" and "boom" in row["error"]
    assert [e.id for e in parts(conn, sid)] == [old.id]
    assert library.get_split(conn, sid).editable  # the admin can fix it and try again


def test_database_failure_during_publish_removes_the_parts(conn, clock, runner, media, monkeypatch):  # NF-8
    sid = published(conn, clock, runner)
    old = parts(conn, sid)[0]
    before = sorted(p.name for p in media.rglob("*") if p.is_file() and ".tmp" not in p.parts)

    def broken(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(library, "mark_split", lambda conn, sid, status, **kw: broken() if status == "done" else None)
    job = split(conn, clock, runner, sid)
    assert job.status == JobStatus.QUEUED and "disk full" in job.error
    assert [e.id for e in parts(conn, sid)] == [old.id]
    assert sorted(p.name for p in media.rglob("*") if p.is_file() and ".tmp" not in p.parts) == before


def test_a_changed_source_file_fails_the_job(conn, clock, runner, media, shorter_clip):
    sid = published(conn, clock, runner)
    approve(conn, clock, sid)
    import shutil
    shutil.copy(shorter_clip, media / source(conn, sid)["file_path"])  # e.g. a redownload shortened it
    job = run_next(runner, clock)
    assert job.status == JobStatus.FAILED  # not retryable
    row = proposal_status(conn, sid)
    assert row["status"] == "failed" and row["error"] == "The last part must end at the end of the video."
    assert len(parts(conn, sid)) == 1


def test_split_without_an_approved_plan_fails(conn, clock, runner):
    sid = published(conn, clock, runner)
    jobs.enqueue(conn, JobType.SPLIT, sid, now=clock.now(), max_attempts=2)
    job = run_next(runner, clock)
    assert job.status == JobStatus.FAILED and "plan" in job.error
    assert len(parts(conn, sid)) == 1


def test_split_with_a_missing_source_file_fails(conn, clock, runner, media):
    sid = published(conn, clock, runner)
    approve(conn, clock, sid)
    (media / source(conn, sid)["file_path"]).unlink()
    job = run_next(runner, clock)
    assert job.status == JobStatus.FAILED
    assert proposal_status(conn, sid)["status"] == "failed"


def test_chapters_prefill_the_split(conn, clock, runner, fake):  # ES-1, SB-1
    from tellybox.ytdlp import Chapter
    fake.download_infos[URL] = info(chapters=[Chapter(0, 8, "Intro"), Chapter(8, 20, "Song")])
    sid = published(conn, clock, runner)
    proposal = library.get_split(conn, sid)
    assert proposal.origin == "chapters"
    assert [(s.start_s, s.title) for s in proposal.segments] == [(0, "Intro"), (8, "Song")]
    assert splitting.cuts_of(proposal.segments) == [8]
