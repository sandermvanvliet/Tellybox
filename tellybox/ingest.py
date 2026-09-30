"""Content ingest: preview, add and the background jobs that download and publish.

Adding a URL after its preview is the admin's approval; nothing else puts
content into the library. Downloads happen in <media>/.tmp/job-<id>/ and only a
verified file is moved into place, together with its database rows (NF-8).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from tellybox import jobs, library, media_format, sponsorblock
from tellybox.clock import Clock
from tellybox.db import to_db
from tellybox.jobs import Job, JobStatus, JobType
from tellybox.media_format import MediaError
from tellybox.ytdlp import PlaylistInfo, VideoInfo, YtDlp, YtDlpError

log = logging.getLogger(__name__)

TMP_DIR = ".tmp"
SHOWS_DIR = "shows"
FILE_MODE = 0o644  # published files must be readable by the web service, whatever user it runs as


class AlreadyAdded(Exception):
    def __init__(self, source_video_id: int, status: str) -> None:
        super().__init__(f"already added (source video {source_video_id}, {status})")
        self.source_video_id = source_video_id
        self.status = status


@dataclass(frozen=True)
class SourceVideo:
    id: int
    youtube_id: str
    url: str
    title: str
    channel_id: str | None
    channel_name: str | None
    duration_s: float | None
    publish: str
    status: str
    show_id: int | None
    file_path: str | None


def get_source_video(conn: sqlite3.Connection, source_id: int) -> SourceVideo | None:
    row = conn.execute(
        """SELECT id, youtube_id, url, title, channel_id, channel_name, duration_s, publish, status, show_id, file_path
           FROM source_video WHERE id = ?""",
        (source_id,),
    ).fetchone()
    return SourceVideo(**dict(row)) if row else None


def _set_source_status(conn: sqlite3.Connection, source_id: int, status: str, now: datetime, error: str | None = None) -> None:
    conn.execute(
        "UPDATE source_video SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, to_db(now), source_id),
    )


# --------------------------------------------------------------------------- admin actions


def preview(ytdlp: YtDlp, url: str) -> VideoInfo:
    """Metadata shown to the admin before anything is downloaded (CI-1)."""
    return ytdlp.preview(url)


def add(conn: sqlite3.Connection, info: VideoInfo, *, publish: bool, now: datetime) -> tuple[int, int]:
    """Approve a previewed video: record it and queue its download. Returns (source_video_id, job_id).

    publish=False holds the episode hidden when ready, e.g. a compilation to split first.
    """
    existing = conn.execute("SELECT id, status FROM source_video WHERE youtube_id = ?", (info.youtube_id,)).fetchone()
    if existing:
        raise AlreadyAdded(existing["id"], existing["status"])
    chapters = json.dumps([asdict(c) for c in info.chapters]) if info.chapters else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """INSERT INTO source_video (youtube_id, url, title, channel_id, channel_name, duration_s, chapters_json,
                 thumbnail_url, publish, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (info.youtube_id, info.url, info.title, info.channel_id, info.channel_name, info.duration_s, chapters,
             info.thumbnail_url, "publish" if publish else "hold", to_db(now), to_db(now)),
        )
        source_id = cur.lastrowid
        job_id = jobs.enqueue(conn, JobType.DOWNLOAD, source_id, now=now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log.info("added %s (%s) as source video %d, job %d", info.youtube_id, info.title, source_id, job_id)
    return source_id, job_id


@dataclass(frozen=True)
class PlaylistAddResult:
    added: list[tuple[int, int]]  # (source_video_id, job_id) per added video, in playlist order
    skipped: list[str]  # youtube ids already in the library, or unavailable


def preview_playlist(ytdlp: YtDlp, url: str) -> PlaylistInfo:
    """Playlist listing shown to the admin before anything is downloaded (CI-7)."""
    return ytdlp.preview_playlist(url)


def existing_youtube_ids(conn: sqlite3.Connection, youtube_ids: list[str]) -> set[str]:
    """Which of these videos are already in the library (any status); the preview greys them out."""
    found: set[str] = set()
    ids = list(dict.fromkeys(youtube_ids))
    for i in range(0, len(ids), 500):  # stay well below SQLite's bound-parameter limit
        chunk = ids[i:i + 500]
        rows = conn.execute(
            f"SELECT youtube_id FROM source_video WHERE youtube_id IN ({', '.join('?' * len(chunk))})", chunk
        )
        found.update(r["youtube_id"] for r in rows)
    return found


def add_playlist(
    conn: sqlite3.Connection, playlist: PlaylistInfo, youtube_ids: list[str], *, publish: bool, now: datetime
) -> PlaylistAddResult:
    """Approve the selected videos of a previewed playlist: one source_video and one DOWNLOAD job each (CI-7).

    One transaction. youtube_ids must be entries of `playlist` (ValueError otherwise). Videos already
    in the library, or unavailable, are skipped rather than raising. Each row records the playlist's
    id and title; its chapters (and a missing channel) are filled in by the download job.
    """
    selected = set(youtube_ids)
    unknown = selected - {e.youtube_id for e in playlist.entries}
    if unknown:
        raise ValueError(f"not in playlist {playlist.playlist_id}: {', '.join(sorted(unknown))}")
    added: list[tuple[int, int]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    publish_value = "publish" if publish else "hold"
    conn.execute("BEGIN IMMEDIATE")
    try:
        for entry in playlist.entries:  # playlist order, whatever order the ids were selected in
            if entry.youtube_id not in selected or entry.youtube_id in seen:
                continue  # a video listed twice in the playlist is added once
            seen.add(entry.youtube_id)
            if entry.unavailable_reason or conn.execute(
                "SELECT 1 FROM source_video WHERE youtube_id = ?", (entry.youtube_id,)
            ).fetchone():
                skipped.append(entry.youtube_id)
                continue
            cur = conn.execute(
                """INSERT INTO source_video (youtube_id, url, title, channel_id, channel_name, duration_s, chapters_json,
                     thumbnail_url, playlist_id, playlist_title, publish, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'queued', ?, ?)""",
                (entry.youtube_id, entry.url, entry.title, entry.channel_id, entry.channel_name, entry.duration_s,
                 entry.thumbnail_url, playlist.playlist_id, playlist.title, publish_value, to_db(now), to_db(now)),
            )
            source_id = cur.lastrowid
            added.append((source_id, jobs.enqueue(conn, JobType.DOWNLOAD, source_id, now=now)))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log.info("added %d videos from playlist %s (%s), skipped %d%s", len(added), playlist.playlist_id,
             playlist.title, len(skipped), "" if publish else ", held")
    return PlaylistAddResult(added=added, skipped=skipped)


def retry(conn: sqlite3.Connection, job_id: int, *, now: datetime) -> Job:
    """Admin retry of a failed job (CI-3)."""
    job = jobs.retry(conn, job_id, now=now)
    if job.type == JobType.DOWNLOAD and job.target_id is not None:
        _set_source_status(conn, job.target_id, "queued", now)
    return job


class RedownloadPending(Exception):
    """A re-check or redownload of this video is already queued or running."""


def request_redownload(conn: sqlite3.Connection, source_id: int, *, with_sponsorblock: bool, now: datetime) -> int:
    """SB-4: "Download again without SponsorBlock" (or with it again). Returns the job id.

    Without: the video is marked admin_off, so the redownload keeps every second and the
    daily re-checks stop. With: the mark is cleared and the re-check window starts again.
    Raises KeyError for an unknown or unpublished video, RedownloadPending when one is queued.
    """
    source = get_source_video(conn, source_id)
    if source is None or source.status != "ready":
        raise KeyError(source_id)
    if jobs.has_pending_for(conn, source_id, (JobType.REDOWNLOAD, JobType.SB_RECHECK)):
        raise RedownloadPending(source_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if with_sponsorblock:
            conn.execute(
                """UPDATE source_video SET sb_status = CASE WHEN sb_status = 'admin_off' THEN NULL ELSE sb_status END,
                     sb_recheck_until = ?, updated_at = ? WHERE id = ?""",
                (to_db(now + timedelta(days=sponsorblock.RECHECK_DAYS)), to_db(now), source_id),
            )
        else:
            conn.execute(
                "UPDATE source_video SET sb_status = 'admin_off', updated_at = ? WHERE id = ?", (to_db(now), source_id)
            )
        job_id = jobs.enqueue(conn, JobType.REDOWNLOAD, source_id, now=now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log.info("redownload of source video %d %s SponsorBlock, job %d", source_id,
             "with" if with_sponsorblock else "without", job_id)
    return job_id


def request_ytdlp_update(conn: sqlite3.Connection, *, now: datetime) -> int | None:
    """Queue a yt-dlp update unless one is pending (CI-5). Returns the job id, or None."""
    if jobs.has_pending(conn, JobType.UPDATE_YTDLP):
        return None
    return jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=now, max_attempts=2)


def record_tool_version(conn: sqlite3.Connection, name: str, version: str, path: Path | None, *, now: datetime) -> None:
    conn.execute(
        """INSERT INTO tool_version (name, version, path, checked_at, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (name) DO UPDATE SET
             updated_at = CASE WHEN tool_version.version = excluded.version THEN tool_version.updated_at ELSE excluded.updated_at END,
             version = excluded.version, path = excluded.path, checked_at = excluded.checked_at""",
        (name, version, str(path) if path else None, to_db(now), to_db(now)),
    )


# --------------------------------------------------------------------------- jobs


@dataclass
class JobRunner:
    """Runs claimed jobs; used by the worker service."""

    conn: sqlite3.Connection
    media_dir: Path
    tools_dir: Path
    ytdlp: YtDlp
    clock: Clock

    def tmp_dir(self, job_id: int) -> Path:
        return self.media_dir / TMP_DIR / f"job-{job_id}"

    def recover(self) -> None:
        """After a worker restart every running job is orphaned (NF-7)."""
        for job in jobs.recover_stale(self.conn, now=self.clock.now(), stale_after_s=0):
            log.info("re-queued job %d after restart", job.id)
            if job.type == JobType.DOWNLOAD and job.target_id is not None:
                _set_source_status(self.conn, job.target_id, "queued", self.clock.now())
        shutil.rmtree(self.media_dir / TMP_DIR, ignore_errors=True)

    def run(self, job: Job) -> None:
        if job.type == JobType.DOWNLOAD:
            self._run_download(job)
        elif job.type == JobType.UPDATE_YTDLP:
            self._run_update(job)
        else:  # pragma: no cover - guarded by the schema
            jobs.fail(self.conn, job.id, f"unknown job type {job.type}", now=self.clock.now(), retryable=False)

    def _progress(self, job_id: int):
        def report(fraction: float) -> None:
            jobs.heartbeat(self.conn, job_id, now=self.clock.now(), progress=round(fraction, 3))
        return report

    # ------------------------------------------------------------ download → process → publish

    def _run_download(self, job: Job) -> None:
        source = get_source_video(self.conn, job.target_id) if job.target_id is not None else None
        if source is None:
            jobs.fail(self.conn, job.id, "source video no longer exists", now=self.clock.now(), retryable=False)
            return
        tmp = self.tmp_dir(job.id)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            _set_source_status(self.conn, source.id, "downloading", self.clock.now())
            result = self.ytdlp.download(source.url, tmp / "download", on_progress=self._progress(job.id))

            jobs.set_status(self.conn, job.id, JobStatus.PROCESSING, now=self.clock.now())
            _set_source_status(self.conn, source.id, "processing", self.clock.now())
            info = media_format.probe(result.video_path)
            plan = media_format.plan_for(info)
            log.info("job %d: %s", job.id, "remux" if plan.remux_only else f"encode ({plan})")
            out = tmp / "episode.mp4"
            media_format.convert(result.video_path, out, plan, duration_s=info.duration_s, on_progress=self._progress(job.id))
            final = media_format.verify(out)

            self._publish(job, source, result.info, out, result.thumbnail_path, final.duration_s)
        except YtDlpError as exc:
            self._failed(job, source, exc.message, retryable=exc.retryable)
        except MediaError as exc:
            self._failed(job, source, f"media: {exc}", retryable=False)
        except Exception as exc:  # unexpected: keep the worker alive, retry within the attempt limit
            log.exception("job %d failed", job.id)
            self._failed(job, source, f"{type(exc).__name__}: {exc}", retryable=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _failed(self, job: Job, source: SourceVideo, error: str, *, retryable: bool) -> None:
        now = self.clock.now()
        updated = jobs.fail(self.conn, job.id, error, now=now, retryable=retryable)
        status = "queued" if updated.status == JobStatus.QUEUED else "failed"
        _set_source_status(self.conn, source.id, status, now, error)
        log.warning("job %d %s: %s", job.id, "will retry" if status == "queued" else "failed", error)

    def _publish(
        self, job: Job, source: SourceVideo, info: VideoInfo, video: Path, thumbnail: Path | None, duration_s: float | None
    ) -> None:
        """Move the verified file into place and create the episode in one step (NF-8, CI-4).

        A video added from a playlist (CI-7) was recorded from a flat listing: its chapters,
        and sometimes its channel, come from the full info the download reports.
        """
        now = self.clock.now()
        if not source.channel_id and info.channel_id:
            source = replace(source, channel_id=info.channel_id, channel_name=source.channel_name or info.channel_name)
        chapters = json.dumps([asdict(c) for c in info.chapters]) if info.chapters else None
        show = library.find_show_by_channel(self.conn, source.channel_id) if source.channel_id else None
        moved: list[Path] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            show_id = show.id if show else library.create_show(
                self.conn, source.channel_name or info.channel_name or source.title, now=now,
                youtube_channel_id=source.channel_id,
            )
            rel_dir = Path(SHOWS_DIR) / str(show_id)
            (self.media_dir / rel_dir).mkdir(parents=True, exist_ok=True)
            rel_video = rel_dir / f"{source.youtube_id}.mp4"
            os.chmod(video, FILE_MODE)  # the converter writes owner-only temp files
            os.replace(video, self.media_dir / rel_video)
            moved.append(self.media_dir / rel_video)
            rel_thumb = None
            if thumbnail and thumbnail.exists():
                rel_thumb = rel_dir / f"{source.youtube_id}.jpg"
                os.chmod(thumbnail, FILE_MODE)
                os.replace(thumbnail, self.media_dir / rel_thumb)
                moved.append(self.media_dir / rel_thumb)

            episode_id = library.add_episode(
                self.conn, show_id, source.title, str(rel_video), now=now,
                duration_s=duration_s or source.duration_s, source_video_id=source.id,
                thumbnail_path=str(rel_thumb) if rel_thumb else None, hidden=source.publish == "hold",
            )
            self.conn.execute(
                """UPDATE source_video SET file_path = ?, thumbnail_path = ?, show_id = ?, duration_s = COALESCE(?, duration_s),
                     channel_id = COALESCE(channel_id, ?), channel_name = COALESCE(channel_name, ?),
                     chapters_json = COALESCE(chapters_json, ?),
                     status = 'ready', error = NULL, updated_at = ? WHERE id = ?""",
                (str(rel_video), str(rel_thumb) if rel_thumb else None, show_id, duration_s,
                 source.channel_id, source.channel_name or info.channel_name, chapters, to_db(now), source.id),
            )
            jobs.complete(self.conn, job.id, now=now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            for path in moved:
                path.unlink(missing_ok=True)
            raise
        log.info("job %d: published episode %d in show %d%s", job.id, episode_id, show_id,
                 " (held)" if source.publish == "hold" else "")

    # ------------------------------------------------------------ yt-dlp update (CI-5)

    def _run_update(self, job: Job) -> None:
        try:
            from tellybox import ytdlp as ytdlp_module

            result = ytdlp_module.update(self.tools_dir)
            record_tool_version(self.conn, "yt-dlp", result.version, result.path, now=self.clock.now())
            jobs.complete(self.conn, job.id, now=self.clock.now())
            log.info("yt-dlp %s%s", result.version, f" (was {result.previous})" if result.changed else " (unchanged)")
        except YtDlpError as exc:
            jobs.fail(self.conn, job.id, exc.message, now=self.clock.now(), retryable=exc.retryable)
        except Exception as exc:
            log.exception("yt-dlp update failed")
            jobs.fail(self.conn, job.id, f"{type(exc).__name__}: {exc}", now=self.clock.now(), retryable=True)
