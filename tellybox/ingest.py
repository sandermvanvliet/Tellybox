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
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from tellybox import jobs, library, media_format, sponsorblock
from tellybox.clock import Clock
from tellybox.db import from_db, to_db
from tellybox.jobs import Job, JobStatus, JobType
from tellybox.media_format import MediaError
from tellybox.sponsorblock import Segment
from tellybox.ytdlp import PlaylistInfo, SponsorBlockUnavailable, VideoInfo, YtDlp, YtDlpError

log = logging.getLogger(__name__)

TMP_DIR = ".tmp"
SHOWS_DIR = "shows"
FILE_MODE = 0o644  # published files must be readable by the web service, whatever user it runs as
DEFER_PLAYING = timedelta(minutes=20)  # a redownload waits this long while its episode is on the TV (SB-3)


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


def _sb_columns(fetched: Fetched, now: datetime) -> tuple[str | None, str, float, str, str | None]:
    """sb_categories, sb_segments_json, sb_removed_s, sb_status, sb_checked_at for a fetched file (SB-1, SB-5)."""
    return (
        sponsorblock.to_csv(fetched.categories) if fetched.categories else None,
        sponsorblock.dumps(fetched.segments),
        sponsorblock.removed_s(fetched.segments),
        fetched.sb_status,
        None if fetched.sb_status == "unreachable" else to_db(now),
    )


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


@dataclass(frozen=True)
class Fetched:
    """A verified episode file in the job's temp dir, and what SponsorBlock did to it (SB-1)."""

    video: Path
    thumbnail: Path | None
    info: VideoInfo
    duration_s: float | None
    segments: list[Segment]
    categories: list[str]  # the lookup that was made; [] when there was none
    sb_status: str  # cut | none | unreachable | off | admin_off


@dataclass
class JobRunner:
    """Runs claimed jobs; used by the worker service.

    now_playing returns the episode id loaded on the TV, or None; it raises when the cast
    service can't be reached. None (the default) means don't check (SB-3).
    """

    conn: sqlite3.Connection
    media_dir: Path
    tools_dir: Path
    ytdlp: YtDlp
    clock: Clock
    now_playing: Callable[[], int | None] | None = None

    def tmp_dir(self, job_id: int) -> Path:
        return self.media_dir / TMP_DIR / f"job-{job_id}"

    def recover(self) -> None:
        """After a worker restart every running job is orphaned (NF-7)."""
        for job in jobs.recover_stale(self.conn, now=self.clock.now(), stale_after_s=0):
            log.info("re-queued job %d after restart", job.id)
            if job.type == JobType.DOWNLOAD and job.target_id is not None:
                _set_source_status(self.conn, job.target_id, "queued", self.clock.now())
        shutil.rmtree(self.media_dir / TMP_DIR, ignore_errors=True)
        for leftover in (self.media_dir / SHOWS_DIR).glob("*/*.mp4.old"):  # a replacement that never committed
            leftover.unlink(missing_ok=True)

    def run(self, job: Job) -> None:
        if job.type == JobType.DOWNLOAD:
            self._run_download(job)
        elif job.type == JobType.SB_RECHECK:
            self._run_sb_recheck(job)
        elif job.type == JobType.REDOWNLOAD:
            self._run_redownload(job)
        elif job.type == JobType.UPDATE_YTDLP:
            self._run_update(job)
        else:  # pragma: no cover - guarded by the schema
            jobs.fail(self.conn, job.id, f"unknown job type {job.type}", now=self.clock.now(), retryable=False)

    def _progress(self, job_id: int):
        def report(fraction: float) -> None:
            jobs.heartbeat(self.conn, job_id, now=self.clock.now(), progress=round(fraction, 3))
        return report

    # ------------------------------------------------------------ download → process → publish

    def _fetch(
        self, job: Job, url: str, tmp: Path, cats: list[str], *, source_id: int | None, admin_off: bool = False
    ) -> Fetched:
        """Download, cut, convert and verify into `tmp`; shared by download and redownload.

        SB-1: `cats` are cut out. `source_id` is set for a first download, whose source status
        follows the job; a redownload leaves it alone. SB-5: with the SponsorBlock API down,
        a first download continues without it (status unreachable), while a redownload raises
        SponsorBlockUnavailable so that the published file stays.
        """
        status = "admin_off" if admin_off else "off" if not cats else "none"
        segments: list[Segment] = []
        if source_id is not None:
            _set_source_status(self.conn, source_id, "downloading", self.clock.now())
        try:
            result = self.ytdlp.download(url, tmp / "download", on_progress=self._progress(job.id), sb_categories=cats or None)
        except SponsorBlockUnavailable as exc:
            if source_id is None:
                raise
            log.warning("job %d: SponsorBlock unreachable, downloading without it: %s", job.id, exc.message)
            result = self.ytdlp.download(url, tmp / "download", on_progress=self._progress(job.id))
            status, cats = "unreachable", []
        else:
            if result.sponsor_segments:
                segments, status = result.sponsor_segments, "cut"

        jobs.set_status(self.conn, job.id, JobStatus.PROCESSING, now=self.clock.now())
        if source_id is not None:
            _set_source_status(self.conn, source_id, "processing", self.clock.now())
        info = media_format.probe(result.video_path)
        plan = media_format.plan_for(info)
        log.info("job %d: %s", job.id, "remux" if plan.remux_only else f"encode ({plan})")
        out = tmp / "episode.mp4"
        media_format.convert(result.video_path, out, plan, duration_s=info.duration_s, on_progress=self._progress(job.id))
        final = media_format.verify(out)
        return Fetched(out, result.thumbnail_path, result.info, final.duration_s, segments, cats, status)

    def _run_download(self, job: Job) -> None:
        source = get_source_video(self.conn, job.target_id) if job.target_id is not None else None
        if source is None:
            jobs.fail(self.conn, job.id, "source video no longer exists", now=self.clock.now(), retryable=False)
            return
        tmp = self.tmp_dir(job.id)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            show = library.find_show_by_channel(self.conn, source.channel_id) if source.channel_id else None
            cats = sponsorblock.effective_categories(self.conn, show.id if show else None)
            fetched = self._fetch(job, source.url, tmp, cats, source_id=source.id)
            self._publish(job, source, fetched)
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

    def _publish(self, job: Job, source: SourceVideo, fetched: Fetched) -> None:
        """Move the verified file into place and create the episode in one step (NF-8, CI-4).

        A video added from a playlist (CI-7) was recorded from a flat listing: its chapters,
        and sometimes its channel, come from the full info the download reports.
        The sponsorblock columns record what was cut and start the re-check window (SB-1, SB-3).
        """
        now = self.clock.now()
        info, video, thumbnail, duration_s = fetched.info, fetched.video, fetched.thumbnail, fetched.duration_s
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
                     sb_categories = ?, sb_segments_json = ?, sb_removed_s = ?, sb_status = ?, sb_checked_at = ?,
                     sb_recheck_until = ?,
                     status = 'ready', error = NULL, updated_at = ? WHERE id = ?""",
                (str(rel_video), str(rel_thumb) if rel_thumb else None, show_id, duration_s,
                 source.channel_id, source.channel_name or info.channel_name, chapters,
                 *_sb_columns(fetched, now),
                 to_db(now + timedelta(days=sponsorblock.RECHECK_DAYS)), to_db(now), source.id),
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

    # ------------------------------------------------------------ SponsorBlock re-check and redownload (SB-3)

    def _run_sb_recheck(self, job: Job) -> None:
        """SB-3: compare the video's current SponsorBlock segments with the cuts in the file.

        Different: queue a redownload. The same: just note the check. Never fails the video (SB-5).
        """
        source = get_source_video(self.conn, job.target_id) if job.target_id is not None else None
        now = self.clock.now()
        row = self.conn.execute(
            "SELECT sb_status, sb_segments_json, sb_recheck_until FROM source_video WHERE id = ?", (job.target_id,)
        ).fetchone()
        if (
            source is None or row is None or source.status != "ready" or row["sb_status"] == "admin_off"
            or row["sb_recheck_until"] is None or from_db(row["sb_recheck_until"]) <= now
            or self._is_split(source.id)  # SB-6: cuts would break the split points
            or jobs.has_pending_for(self.conn, source.id, (JobType.REDOWNLOAD,))
        ):
            jobs.complete(self.conn, job.id, now=now)
            return
        try:
            cats = sponsorblock.effective_categories(self.conn, source.show_id)
            wanted = self.ytdlp.sponsor_segments(source.url, cats) if cats else []
            if not sponsorblock.same_segments(wanted, sponsorblock.loads(row["sb_segments_json"])):
                jobs.enqueue(self.conn, JobType.REDOWNLOAD, source.id, now=now)
                log.info("job %d: SponsorBlock segments of source video %d changed, redownload queued", job.id, source.id)
            else:
                status = "off" if not cats else "cut" if wanted else "none"  # also catches up an unreachable one
                self.conn.execute(
                    "UPDATE source_video SET sb_categories = ?, sb_status = ?, sb_checked_at = ?, updated_at = ? WHERE id = ?",
                    (sponsorblock.to_csv(cats) if cats else None, status, to_db(now), to_db(now), source.id),
                )
            jobs.complete(self.conn, job.id, now=now)
        except SponsorBlockUnavailable as exc:
            log.info("job %d: SponsorBlock unreachable, the next daily check catches up: %s", job.id, exc.message)
            jobs.complete(self.conn, job.id, now=now)
        except YtDlpError as exc:
            jobs.fail(self.conn, job.id, exc.message, now=now, retryable=exc.retryable)
        except Exception as exc:
            log.exception("job %d failed", job.id)
            jobs.fail(self.conn, job.id, f"{type(exc).__name__}: {exc}", now=now, retryable=True)

    def _is_split(self, source_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM episode WHERE source_video_id = ? AND start_s IS NOT NULL LIMIT 1", (source_id,)
        ).fetchone() is not None

    def _defer_if_playing(self, job: Job, episode_ids: list[int]) -> bool:
        """SB-3: never replace a file that may be on the TV. Unknown (cast service unreachable) counts as playing."""
        if self.now_playing is None:
            return False
        try:
            playing = self.now_playing()
        except Exception as exc:
            reason = f"waiting: the cast service can't be reached ({type(exc).__name__})"
        else:
            if playing is None or playing not in episode_ids:
                return False
            reason = "waiting: the episode is loaded on the TV"
        now = self.clock.now()
        jobs.defer(self.conn, job.id, now + DEFER_PLAYING, reason, now=now)
        log.info("job %d deferred: %s", job.id, reason)
        return True

    def _run_redownload(self, job: Job) -> None:
        """SB-3, SB-4: replace a published file, keeping the episode (id, title, thumbnail, hidden, show).

        The source stays 'ready' and playable throughout; any failure leaves the old file and rows.
        """
        source = get_source_video(self.conn, job.target_id) if job.target_id is not None else None
        if source is None or source.status != "ready" or not source.file_path:
            jobs.fail(self.conn, job.id, "source video is not published", now=self.clock.now(), retryable=False)
            return
        episodes = [r["id"] for r in self.conn.execute(
            "SELECT id FROM episode WHERE source_video_id = ? AND start_s IS NULL", (source.id,))]
        if self._is_split(source.id):  # SB-6
            jobs.complete(self.conn, job.id, now=self.clock.now())
            return
        if self._defer_if_playing(job, episodes):
            return
        tmp = self.tmp_dir(job.id)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            admin_off = self.conn.execute(
                "SELECT sb_status FROM source_video WHERE id = ?", (source.id,)).fetchone()[0] == "admin_off"
            cats = [] if admin_off else sponsorblock.effective_categories(self.conn, source.show_id)
            fetched = self._fetch(job, source.url, tmp, cats, source_id=None, admin_off=admin_off)
            if self._defer_if_playing(job, episodes):
                return
            self._swap(job, source, episodes, fetched)
        except YtDlpError as exc:
            jobs.fail(self.conn, job.id, exc.message, now=self.clock.now(), retryable=exc.retryable)
        except MediaError as exc:
            jobs.fail(self.conn, job.id, f"media: {exc}", now=self.clock.now(), retryable=False)
        except Exception as exc:
            log.exception("job %d failed", job.id)
            jobs.fail(self.conn, job.id, f"{type(exc).__name__}: {exc}", now=self.clock.now(), retryable=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _swap(self, job: Job, source: SourceVideo, episode_ids: list[int], fetched: Fetched) -> None:
        """Put the new file at the old path with the new duration and position shifts in one step (NF-8)."""
        now = self.clock.now()
        old = sponsorblock.loads(self.conn.execute(
            "SELECT sb_segments_json FROM source_video WHERE id = ?", (source.id,)).fetchone()[0])
        changed = not sponsorblock.same_segments(old, fetched.segments)
        target = self.media_dir / source.file_path
        backup = target.with_name(target.name + ".old")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Keep the old file until the commit: a failure restores it rather than losing it.
            os.link(target, backup)
            os.chmod(fetched.video, FILE_MODE)
            os.replace(fetched.video, target)
            for episode_id in episode_ids:
                self.conn.execute("UPDATE episode SET duration_s = COALESCE(?, duration_s) WHERE id = ?",
                                  (fetched.duration_s, episode_id))
                if changed:
                    self.conn.execute(
                        """INSERT INTO position_shift (episode_id, old_cuts_json, new_cuts_json, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (episode_id, sponsorblock.dumps(old), sponsorblock.dumps(fetched.segments), to_db(now)),
                    )
            self.conn.execute(
                """UPDATE source_video SET sb_categories = ?, sb_segments_json = ?, sb_removed_s = ?, sb_status = ?,
                     sb_checked_at = ?, updated_at = ? WHERE id = ?""",
                (*_sb_columns(fetched, now), to_db(now), source.id),
            )
            jobs.complete(self.conn, job.id, now=now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            if backup.exists():
                os.replace(backup, target)
            raise
        backup.unlink(missing_ok=True)
        log.info("job %d: replaced the file of source video %d (%s)", job.id, source.id,
                 f"{sponsorblock.removed_s(fetched.segments):.0f}s cut" if fetched.segments else fetched.sb_status)

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
