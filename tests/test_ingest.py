"""Ingest: add, download job, publish, failures (CI-1..CI-4, NF-7, NF-8). No network."""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tellybox import ingest, jobs, library, ytdlp
from tellybox.clock import FakeClock
from tellybox.db import open_db, to_db
from tellybox.ingest import JobRunner
from tellybox.jobs import JobStatus, JobType
from tellybox.ytdlp import Chapter, DownloadResult, UpdateResult, VideoInfo, YtDlpError


def _ffmpeg(out: Path, *args: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
         *args, str(out)],
        check=True,
    )
    return out


@pytest.fixture(scope="session")
def h264_clip(tmp_path_factory) -> Path:
    return _ffmpeg(tmp_path_factory.mktemp("clips") / "h264.mp4",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac")


@pytest.fixture(scope="session")
def vp9_clip(tmp_path_factory) -> Path:
    return _ffmpeg(tmp_path_factory.mktemp("clips") / "vp9.webm",
                   "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "200k", "-c:a", "libopus")


def info(youtube_id="abc123", channel_id="UCkids", channel_name="Kids Channel", title="Episode one", **kw) -> VideoInfo:
    return VideoInfo(
        youtube_id=youtube_id, url=f"https://www.youtube.com/watch?v={youtube_id}", title=title,
        channel_id=channel_id, channel_name=channel_name, duration_s=2.0,
        thumbnail_url=f"https://i.ytimg.com/vi/{youtube_id}/hq.jpg",
        chapters=kw.pop("chapters", []), is_live=False, **kw,
    )


class FakeYtDlp:
    """Stands in for tellybox.ytdlp.YtDlp; 'downloads' a local clip."""

    def __init__(self, clip: Path, infos: dict[str, VideoInfo] | None = None) -> None:
        self.clip = clip
        self.infos = infos or {}
        self.download_infos: dict[str, VideoInfo] = {}  # full info the download reports, by URL
        self.error: YtDlpError | None = None
        self.downloads: list[str] = []

    def preview(self, url: str) -> VideoInfo:
        return self.infos[url]

    def download(self, url, dest_dir, *, on_progress=None, timeout=0, sb_categories=None):
        self.downloads.append(url)
        if self.error:
            raise self.error
        dest_dir.mkdir(parents=True, exist_ok=True)
        video = dest_dir / f"video{self.clip.suffix}"
        shutil.copy(self.clip, video)
        thumb = dest_dir / "thumb.jpg"
        thumb.write_bytes(b"\xff\xd8fakejpeg")
        if on_progress:
            for p in (0.25, 0.5, 1.0):
                on_progress(p)
        youtube_id = url.rsplit("=", 1)[-1]
        return DownloadResult(video_path=video, thumbnail_path=thumb,
                              info=self.download_infos.get(url) or info(youtube_id))

    def version(self):
        return "2026.08.19"

    def active_dir(self):
        return None


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 9, 28, 10, 0, tzinfo=UTC))


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "t.db")


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def fake(h264_clip):
    return FakeYtDlp(h264_clip)


@pytest.fixture
def runner(conn, media, tmp_path, fake, clock):
    return JobRunner(conn=conn, media_dir=media, tools_dir=tmp_path / "tools", ytdlp=fake, clock=clock)


def run_next(runner, clock):
    job = jobs.claim_next(runner.conn, now=clock.now())
    assert job is not None
    runner.run(job)
    return jobs.get(runner.conn, job.id)


def source(conn, source_id):
    return conn.execute("SELECT * FROM source_video WHERE id = ?", (source_id,)).fetchone()


# --------------------------------------------------------------------------- add


def test_add_records_source_and_queues_download(conn, clock):  # CI-1, CI-3
    source_id, job_id = ingest.add(conn, info(chapters=[Chapter(0, 60, "Intro")]), publish=True, now=clock.now())
    row = source(conn, source_id)
    assert row["status"] == "queued" and row["publish"] == "publish"
    assert '"title": "Intro"' in row["chapters_json"]  # kept for chapter import (ES-1)
    job = jobs.get(conn, job_id)
    assert (job.type, job.target_id, job.status) == (JobType.DOWNLOAD, source_id, JobStatus.QUEUED)
    assert conn.execute("SELECT count(*) FROM episode").fetchone()[0] == 0  # nothing visible yet


def test_add_twice_is_refused(conn, clock):
    source_id, _ = ingest.add(conn, info(), publish=True, now=clock.now())
    with pytest.raises(ingest.AlreadyAdded) as exc:
        ingest.add(conn, info(), publish=True, now=clock.now())
    assert exc.value.source_video_id == source_id


# --------------------------------------------------------------------------- playlists (CI-7)

FIXTURES = Path(__file__).parent / "fixtures" / "ytdlp"


@pytest.fixture
def playlist():
    # vid..1, vid..2 addable; vid..3 private, 4 deleted, 5 upcoming, 6 members-only, 7 live
    return ytdlp.parse_playlist(json.loads((FIXTURES / "playlist_flat.json").read_text()))


def test_existing_youtube_ids(conn, clock):
    ingest.add(conn, info("aaa"), publish=True, now=clock.now())
    assert ingest.existing_youtube_ids(conn, ["aaa", "bbb"]) == {"aaa"}
    assert ingest.existing_youtube_ids(conn, []) == set()


def test_add_playlist_queues_one_job_per_video(conn, clock, playlist):
    result = ingest.add_playlist(conn, playlist, ["vid00000002", "vid00000001"], publish=False, now=clock.now())
    assert result.skipped == []
    assert len(result.added) == 2
    rows = [source(conn, sid) for sid, _ in result.added]
    assert [r["youtube_id"] for r in rows] == ["vid00000001", "vid00000002"]  # playlist order, not selection order
    first = rows[0]
    assert first["url"] == "https://www.youtube.com/watch?v=vid00000001"
    assert (first["title"], first["channel_id"], first["channel_name"], first["duration_s"]) == (
        "Tractor Tom - Episode 1", "UCtractor", "Tractor Tom Official", 661.0)
    assert first["thumbnail_url"].endswith("sqp=large")
    assert (first["playlist_id"], first["playlist_title"]) == ("PLtractor123", "Tractor Tom - Full Episodes")
    assert first["chapters_json"] is None  # filled in by the download
    assert {r["publish"] for r in rows} == {"hold"} and {r["status"] for r in rows} == {"queued"}
    assert rows[1]["channel_id"] == "UCpeppa"  # each video keeps its own channel (CI-4)
    for sid, job_id in result.added:
        job = jobs.get(conn, job_id)
        assert (job.type, job.target_id, job.status) == (JobType.DOWNLOAD, sid, JobStatus.QUEUED)
    assert conn.execute("SELECT count(*) FROM job").fetchone()[0] == 2


def test_add_playlist_publish(conn, clock, playlist):
    result = ingest.add_playlist(conn, playlist, ["vid00000001"], publish=True, now=clock.now())
    assert source(conn, result.added[0][0])["publish"] == "publish"


def test_add_playlist_skips_existing_and_unavailable(conn, clock, playlist):
    ingest.add(conn, info("vid00000002"), publish=True, now=clock.now())
    every = [e.youtube_id for e in playlist.entries]
    result = ingest.add_playlist(conn, playlist, every, publish=False, now=clock.now())
    assert [source(conn, sid)["youtube_id"] for sid, _ in result.added] == ["vid00000001"]
    assert result.skipped == ["vid00000002", "vid00000003", "vid00000004", "vid00000005", "vid00000006", "vid00000007"]
    assert conn.execute("SELECT count(*) FROM job").fetchone()[0] == 2  # the earlier add plus one


def test_add_playlist_twice_adds_nothing_new(conn, clock, playlist):  # "add the rest later"
    ingest.add_playlist(conn, playlist, ["vid00000001"], publish=False, now=clock.now())
    result = ingest.add_playlist(conn, playlist, ["vid00000001", "vid00000002"], publish=False, now=clock.now())
    assert [source(conn, sid)["youtube_id"] for sid, _ in result.added] == ["vid00000002"]
    assert result.skipped == ["vid00000001"]


def test_add_playlist_rejects_ids_outside_the_playlist(conn, clock, playlist):
    with pytest.raises(ValueError):
        ingest.add_playlist(conn, playlist, ["vid00000001", "notinlist"], publish=False, now=clock.now())
    assert conn.execute("SELECT count(*) FROM source_video").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM job").fetchone()[0] == 0


def test_add_playlist_rolls_back_on_error(conn, clock, playlist, monkeypatch):
    real = jobs.enqueue
    calls = []

    def enqueue(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(*a, **kw)

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    with pytest.raises(RuntimeError):
        ingest.add_playlist(conn, playlist, ["vid00000001", "vid00000002"], publish=False, now=clock.now())
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM source_video").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM job").fetchone()[0] == 0


def test_download_fills_chapters_and_channel_for_playlist_videos(conn, clock, runner, fake, playlist):  # ES-1, CI-4
    result = ingest.add_playlist(conn, playlist, ["vid00000001"], publish=False, now=clock.now())
    source_id = result.added[0][0]
    conn.execute("UPDATE source_video SET channel_id = NULL, channel_name = NULL WHERE id = ?", (source_id,))
    fake.download_infos["https://www.youtube.com/watch?v=vid00000001"] = info(
        "vid00000001", channel_id="UCtractor", channel_name="Tractor Tom Official",
        chapters=[Chapter(0, 300, "Part 1"), Chapter(300, 661, "Part 2")],
    )
    assert run_next(runner, clock).status == JobStatus.READY
    row = source(conn, source_id)
    assert json.loads(row["chapters_json"]) == [
        {"start_s": 0, "end_s": 300, "title": "Part 1"}, {"start_s": 300, "end_s": 661, "title": "Part 2"}]
    assert (row["channel_id"], row["channel_name"]) == ("UCtractor", "Tractor Tom Official")
    show = library.find_show_by_channel(conn, "UCtractor")  # grouped by the downloaded channel (CI-4)
    assert show is not None and show.name == "Tractor Tom Official" and row["show_id"] == show.id
    (episode,) = library.list_episodes(conn, show.id, include_hidden=True)
    assert episode.hidden  # held for approval


def test_download_keeps_existing_chapters_and_channel(conn, clock, runner, fake):
    source_id, _ = ingest.add(conn, info("abc123", chapters=[Chapter(0, 1, "Mine")]), publish=True, now=clock.now())
    fake.download_infos["https://www.youtube.com/watch?v=abc123"] = info(
        "abc123", channel_id="UCother", channel_name="Other", chapters=[Chapter(0, 2, "Theirs")])
    run_next(runner, clock)
    row = source(conn, source_id)
    assert json.loads(row["chapters_json"])[0]["title"] == "Mine"
    assert row["channel_id"] == "UCkids"


# --------------------------------------------------------------------------- download job


def test_download_publishes_episode_in_channel_show(conn, clock, runner, media):  # CI-2, CI-4
    source_id, _ = ingest.add(conn, info(), publish=True, now=clock.now())
    job = run_next(runner, clock)
    assert job.status == JobStatus.READY and job.progress == 1

    row = source(conn, source_id)
    assert row["status"] == "ready"
    show = library.find_show_by_channel(conn, "UCkids")
    assert show.name == "Kids Channel"
    (episode,) = library.list_episodes(conn, show.id)
    assert episode.title == "Episode one" and not episode.hidden
    assert episode.source_video_id == source_id
    assert episode.file_path == f"shows/{show.id}/abc123.mp4" == row["file_path"]
    assert (media / episode.file_path).is_file()
    assert (media / f"shows/{show.id}/abc123.jpg").is_file()
    assert not (media / ingest.TMP_DIR / f"job-{job.id}").exists()
    for rel in (episode.file_path, f"shows/{show.id}/abc123.jpg"):
        assert (media / rel).stat().st_mode & 0o777 == 0o644  # readable by the web service


def test_same_channel_reuses_show(conn, clock, runner):  # CI-4
    ingest.add(conn, info("aaa"), publish=True, now=clock.now())
    ingest.add(conn, info("bbb", title="Episode two"), publish=True, now=clock.now())
    run_next(runner, clock)
    run_next(runner, clock)
    assert conn.execute("SELECT count(*) FROM show").fetchone()[0] == 1
    show = library.find_show_by_channel(conn, "UCkids")
    assert [e.title for e in library.list_episodes(conn, show.id)] == ["Episode one", "Episode two"]


def test_hold_keeps_episode_hidden(conn, clock, runner):  # "Nothing reaches the kid app without approval"
    ingest.add(conn, info(), publish=False, now=clock.now())
    run_next(runner, clock)
    (episode,) = library.list_episodes(conn, library.find_show_by_channel(conn, "UCkids").id, include_hidden=True)
    assert episode.hidden


def test_non_h264_source_is_encoded(conn, clock, runner, fake, vp9_clip, media):  # CI-2
    fake.clip = vp9_clip
    ingest.add(conn, info(), publish=True, now=clock.now())
    assert run_next(runner, clock).status == JobStatus.READY
    from tellybox import media_format
    (episode,) = library.list_episodes(conn, library.find_show_by_channel(conn, "UCkids").id)
    verified = media_format.verify(media / episode.file_path)
    assert (verified.video_codec, verified.audio_codec) == ("h264", "aac")


def test_progress_is_reported(conn, clock, runner, monkeypatch):  # CI-3
    seen = []
    real = jobs.heartbeat
    monkeypatch.setattr(jobs, "heartbeat", lambda c, i, *, now, progress=None: (seen.append(progress), real(c, i, now=now, progress=progress)))
    ingest.add(conn, info(), publish=True, now=clock.now())
    run_next(runner, clock)
    assert 0.5 in seen


# --------------------------------------------------------------------------- failures (NF-8)


def _nothing_published(conn, media):
    assert conn.execute("SELECT count(*) FROM episode").fetchone()[0] == 0
    files = [p for p in media.rglob("*") if p.is_file()]
    assert files == []


def test_permanent_download_error_fails_without_publishing(conn, clock, runner, fake, media):
    fake.error = YtDlpError("Video unavailable", retryable=False)
    source_id, _ = ingest.add(conn, info(), publish=True, now=clock.now())
    job = run_next(runner, clock)
    assert job.status == JobStatus.FAILED and job.error == "Video unavailable"
    row = source(conn, source_id)
    assert row["status"] == "failed" and row["error"] == "Video unavailable"
    _nothing_published(conn, media)


def test_retryable_download_error_is_requeued_with_backoff(conn, clock, runner, fake):
    fake.error = YtDlpError("HTTP Error 503", retryable=True)
    source_id, _ = ingest.add(conn, info(), publish=True, now=clock.now())
    job = run_next(runner, clock)
    assert job.status == JobStatus.QUEUED and job.run_after > clock.now()
    assert source(conn, source_id)["status"] == "queued"
    assert jobs.claim_next(conn, now=clock.now()) is None  # waits for the backoff
    fake.error = None
    clock.advance(jobs.BACKOFF_S[0])
    assert run_next(runner, clock).status == JobStatus.READY


def test_unreadable_media_fails_without_publishing(conn, clock, runner, fake, tmp_path, media):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    fake.clip = junk
    ingest.add(conn, info(), publish=True, now=clock.now())
    job = run_next(runner, clock)
    assert job.status == JobStatus.FAILED and job.error.startswith("media:")
    _nothing_published(conn, media)


def test_database_failure_during_publish_removes_moved_files(conn, clock, runner, media, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(library, "add_episode", boom)
    ingest.add(conn, info(), publish=True, now=clock.now())
    job = run_next(runner, clock)
    assert job.status == JobStatus.QUEUED and "disk full" in job.error  # unexpected errors retry
    _nothing_published(conn, media)
    assert conn.execute("SELECT count(*) FROM show").fetchone()[0] == 0  # rolled back


def test_admin_retry_after_failure(conn, clock, runner, fake):  # CI-3
    fake.error = YtDlpError("Private video", retryable=False)
    source_id, job_id = ingest.add(conn, info(), publish=True, now=clock.now())
    run_next(runner, clock)
    fake.error = None
    ingest.retry(conn, job_id, now=clock.now())
    assert source(conn, source_id)["status"] == "queued"
    assert run_next(runner, clock).status == JobStatus.READY
    assert source(conn, source_id)["status"] == "ready"


def test_recover_requeues_running_jobs_and_cleans_temp(conn, clock, runner, media):  # NF-7
    source_id, job_id = ingest.add(conn, info(), publish=True, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    leftover = runner.tmp_dir(job_id) / "download" / "video.mp4.part"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"partial")
    runner.recover()  # worker restarted right away: heartbeat is fresh but the job is orphaned
    assert jobs.get(conn, job_id).status == JobStatus.QUEUED
    assert source(conn, source_id)["status"] == "queued"
    assert not (media / ingest.TMP_DIR).exists()
    assert run_next(runner, clock).status == JobStatus.READY


# --------------------------------------------------------------------------- yt-dlp update (CI-5)


def test_update_job_records_version(conn, clock, runner, monkeypatch, tmp_path):
    from tellybox import ytdlp as ytdlp_module
    monkeypatch.setattr(ytdlp_module, "update", lambda tools_dir, **kw: UpdateResult(
        version="2026.09.01", previous="2026.08.19", changed=True, path=tools_dir / "versions" / "x"))
    job_id = ingest.request_ytdlp_update(conn, now=clock.now())
    assert ingest.request_ytdlp_update(conn, now=clock.now()) is None  # one pending at a time
    assert run_next(runner, clock).status == JobStatus.READY
    row = conn.execute("SELECT version FROM tool_version WHERE name = 'yt-dlp'").fetchone()
    assert row["version"] == "2026.09.01"
    assert jobs.get(conn, job_id).status == JobStatus.READY


def test_failed_update_is_retried_once(conn, clock, runner, monkeypatch):
    from tellybox import ytdlp as ytdlp_module

    def fail(tools_dir, **kw):
        raise YtDlpError("pip failed", retryable=True)
    monkeypatch.setattr(ytdlp_module, "update", fail)
    job_id = ingest.request_ytdlp_update(conn, now=clock.now())
    assert run_next(runner, clock).status == JobStatus.QUEUED
    clock.advance(jobs.BACKOFF_S[0])
    assert run_next(runner, clock).status == JobStatus.FAILED
    assert jobs.get(conn, job_id).attempts == 2


# --------------------------------------------------------------------------- SponsorBlock admin actions (SB-4)


def test_request_redownload(conn, clock, runner):
    sid, _ = ingest.add(conn, info(), publish=True, now=clock.now())
    with pytest.raises(KeyError):  # not published yet
        ingest.request_redownload(conn, sid, with_sponsorblock=False, now=clock.now())
    run_next(runner, clock)
    job_id = ingest.request_redownload(conn, sid, with_sponsorblock=False, now=clock.now())
    job = jobs.get(conn, job_id)
    assert (job.type, job.target_id, job.status) == (JobType.REDOWNLOAD, sid, JobStatus.QUEUED)
    assert source(conn, sid)["sb_status"] == "admin_off"
    with pytest.raises(ingest.RedownloadPending):
        ingest.request_redownload(conn, sid, with_sponsorblock=True, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    jobs.complete(conn, job_id, now=clock.now())
    ingest.request_redownload(conn, sid, with_sponsorblock=True, now=clock.now())
    row = source(conn, sid)
    assert row["sb_status"] is None
    assert row["sb_recheck_until"] == to_db(clock.now() + timedelta(days=7))
    with pytest.raises(KeyError):
        ingest.request_redownload(conn, 999, with_sponsorblock=True, now=clock.now())
