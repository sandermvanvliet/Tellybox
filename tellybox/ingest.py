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

from tellybox import images, jobs, library, media_format, sponsorblock, splitting
from tellybox.clock import Clock
from tellybox.db import from_db, to_db
from tellybox.jobs import Job, JobStatus, JobType
from tellybox.images import ImageError
from tellybox.media_format import MediaError
from tellybox.sponsorblock import Segment
from tellybox.ytdlp import PlaylistInfo, SponsorBlockUnavailable, VideoInfo, YtDlp, YtDlpError

log = logging.getLogger(__name__)

TMP_DIR = ".tmp"
SHOWS_DIR = "shows"
FILE_MODE = 0o644  # published files must be readable by the web service, whatever user it runs as
DEFER_PLAYING = timedelta(minutes=20)  # a redownload waits this long while its episode is on the TV (SB-3)
AUTO_DETECT_FACTOR = 1.5  # ES-10: a new video longer than this many episode lengths is a compilation
AUTO_DETECT_NO_HINT_S = 20 * 60  # ES-10: without a length hint, longer than this


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
    awaiting_split: bool = False  # ES-10, A-22


def get_source_video(conn: sqlite3.Connection, source_id: int) -> SourceVideo | None:
    row = conn.execute(
        """SELECT id, youtube_id, url, title, channel_id, channel_name, duration_s, publish, status, show_id, file_path,
                  awaiting_split
           FROM source_video WHERE id = ?""",
        (source_id,),
    ).fetchone()
    return SourceVideo(**{**dict(row), "awaiting_split": bool(row["awaiting_split"])}) if row else None


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


class SourceSplit(Exception):
    """The video is split, or being split: its file must not change (SB-6)."""


def request_redownload(conn: sqlite3.Connection, source_id: int, *, with_sponsorblock: bool, now: datetime) -> int:
    """SB-4: "Download again without SponsorBlock" (or with it again). Returns the job id.

    Without: the video is marked admin_off, so the redownload keeps every second and the
    daily re-checks stop. With: the mark is cleared and the re-check window starts again.
    Raises KeyError for an unknown or unpublished video, RedownloadPending when one is queued,
    SourceSplit when the video is split or a split is queued (SB-6).
    """
    source = get_source_video(conn, source_id)
    if source is None or source.status != "ready":
        raise KeyError(source_id)
    if library.is_split(conn, source_id) or jobs.has_pending_for(conn, source_id, (JobType.SPLIT,)):
        raise SourceSplit(source_id)
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


def _usable_cuts(cuts: list, duration_s: float) -> list:
    """Drop cuts that would leave a kept part shorter than MIN_KEPT_S; that part merges into the previous one."""
    kept: list = []
    prev = 0.0
    for cut in cuts:
        if cut.at_s - prev >= splitting.MIN_KEPT_S:
            kept.append(cut)
            prev = cut.at_s
    while kept and duration_s - kept[-1].at_s < splitting.MIN_KEPT_S:
        kept.pop()
    return kept


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
        elif job.type == JobType.SPLIT:
            self._run_split(job)
        elif job.type == JobType.DETECT:
            self._run_detect(job)
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
            # ES-10, A-22: a long video of a show set to auto-detect stays hidden until its split is decided.
            awaiting = self._is_compilation(show_id, duration_s or source.duration_s)
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
                thumbnail_path=str(rel_thumb) if rel_thumb else None, hidden=source.publish == "hold" or awaiting,
            )
            self.conn.execute(
                """UPDATE source_video SET file_path = ?, thumbnail_path = ?, show_id = ?, duration_s = COALESCE(?, duration_s),
                     channel_id = COALESCE(channel_id, ?), channel_name = COALESCE(channel_name, ?),
                     chapters_json = COALESCE(?, chapters_json),
                     sb_categories = ?, sb_segments_json = ?, sb_removed_s = ?, sb_status = ?, sb_checked_at = ?,
                     sb_recheck_until = ?, awaiting_split = ?,
                     status = 'ready', error = NULL, updated_at = ? WHERE id = ?""",
                (str(rel_video), str(rel_thumb) if rel_thumb else None, show_id, duration_s,
                 source.channel_id, source.channel_name or info.channel_name, chapters,
                 *_sb_columns(fetched, now),
                 to_db(now + timedelta(days=sponsorblock.RECHECK_DAYS)), int(awaiting), to_db(now), source.id),
            )
            jobs.complete(self.conn, job.id, now=now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            for path in moved:
                path.unlink(missing_ok=True)
            raise
        log.info("job %d: published episode %d in show %d%s", job.id, episode_id, show_id,
                 " (held)" if source.publish == "hold" else " (awaiting its split)" if awaiting else "")
        if awaiting:
            try:
                library.request_detect(self.conn, source.id, now=self.clock.now())
            except (LookupError, library.SplitLocked, library.SourceGone) as exc:
                log.info("job %d: no detection queued for source video %d: %r", job.id, source.id, exc)

    def _is_compilation(self, show_id: int, duration_s: float | None) -> bool:
        """ES-10: the show wants auto-detection and the video is longer than 1.5 episodes (20 min without a hint)."""
        profile = library.get_split_profile(self.conn, show_id)
        if not (profile.usable and profile.auto_detect and duration_s):
            return False
        limit = AUTO_DETECT_FACTOR * profile.length_hint_s if profile.length_hint_s else AUTO_DETECT_NO_HINT_S
        return duration_s > limit

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
            if source is not None and self._is_split(source.id):
                log.info("job %d: source video %d is split, no SponsorBlock re-check (SB-6)", job.id, source.id)
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
        """SB-6: split, or about to be: a replaced file would break the cut points."""
        return library.is_split(self.conn, source_id) or jobs.has_pending_for(self.conn, source_id, (JobType.SPLIT,))

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
            log.info("job %d: source video %d is split, not downloading again (SB-6)", job.id, source.id)
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
                     sb_checked_at = ?, chapters_json = COALESCE(?, chapters_json), updated_at = ? WHERE id = ?""",
                (*_sb_columns(fetched, now),
                 json.dumps([asdict(c) for c in fetched.info.chapters]) if fetched.info.chapters else None,
                 to_db(now), source.id),
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

    # ------------------------------------------------------------ splitting (ES-8)

    def _split_failed(self, job: Job, source_id: int, error: str, *, retryable: bool) -> None:
        """Fail the job; the proposal follows: back to approved while a retry is queued, else failed."""
        now = self.clock.now()
        updated = jobs.fail(self.conn, job.id, error, now=now, retryable=retryable)
        if updated.status == JobStatus.QUEUED:
            self.conn.execute(
                "UPDATE split_proposal SET status = 'approved', updated_at = ? WHERE source_video_id = ?",
                (to_db(now), source_id),
            )
        else:
            library.mark_split(self.conn, source_id, "failed", now=now, error=error)
        log.warning("job %d %s: %s", job.id, "will retry" if updated.status == JobStatus.QUEUED else "failed", error)

    def _defer_split(self, job: Job, source_id: int, episode_ids: list[int]) -> bool:
        """Like _defer_if_playing, for a job that already marked its proposal as cutting."""
        if not self._defer_if_playing(job, episode_ids):
            return False
        self.conn.execute(
            "UPDATE split_proposal SET status = 'approved', updated_at = ? WHERE source_video_id = ?",
            (to_db(self.clock.now()), source_id),
        )
        return True

    def _run_split(self, job: Job) -> None:
        """ES-8: cut the approved plan into episodes that replace the source's current ones.

        The old episodes stay playable until the one transaction that swaps them for the parts (NF-8).
        """
        now = self.clock.now()
        source = get_source_video(self.conn, job.target_id) if job.target_id is not None else None
        proposal = library.get_split(self.conn, source.id) if source else None
        if source is None or proposal is None or not proposal.stored or proposal.status not in ("approved", "cutting"):
            jobs.fail(self.conn, job.id, "no approved split plan for this video", now=now, retryable=False)
            return
        src_path = self.media_dir / source.file_path if source.file_path else None
        if src_path is None or not src_path.is_file():
            error = "the source video file is gone"
            jobs.fail(self.conn, job.id, error, now=now, retryable=False)
            library.mark_split(self.conn, source.id, "failed", now=now, error=error)
            return
        episode_ids = [e.id for e in library.list_source_episodes(self.conn, source.id)]
        if self._defer_if_playing(job, episode_ids):
            return
        library.mark_split(self.conn, source.id, "cutting", now=now)
        tmp = self.tmp_dir(job.id)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            duration = media_format.probe(src_path).duration_s or proposal.duration_s
            splitting.validate(proposal.segments, duration)  # the file may differ from what the plan was made on
            kept = [s for s in proposal.segments if s.keep]
            total = sum(s.length_s for s in kept)
            report = self._progress(job.id)
            parts: list[tuple[Path, Path, float]] = []  # (video, thumbnail, duration)
            done_s = 0.0
            for n, seg in enumerate(kept, 1):
                part = tmp / f"part-{n:02d}.mp4"
                end = min(seg.end_s, duration)
                media_format.cut(
                    src_path, part, seg.start_s, end,
                    on_progress=lambda f, base=done_s, seg=seg: report((base + f * seg.length_s) / total),
                )
                length = media_format.verify(part).duration_s or end - seg.start_s
                thumb = tmp / f"part-{n:02d}.jpg"
                thumb.write_bytes(images.grab_frame(part, min(3.0, length / 4)))
                parts.append((part, thumb, length))
                done_s += seg.length_s
            if self._defer_split(job, source.id, episode_ids):  # a part may have been picked during the cut
                return
            self._publish_split(job, source, proposal, kept, parts, duration)
        except splitting.SplitInvalid as exc:
            self._split_failed(job, source.id, str(exc), retryable=False)
        except MediaError as exc:
            self._split_failed(job, source.id, f"media: {exc}", retryable=True)
        except ImageError as exc:
            self._split_failed(job, source.id, f"image: {exc}", retryable=True)
        except Exception as exc:
            log.exception("job %d failed", job.id)
            self._split_failed(job, source.id, f"{type(exc).__name__}: {exc}", retryable=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _publish_split(
        self, job: Job, source: SourceVideo, proposal: library.SplitProposal, kept: list[splitting.Segment],
        parts: list[tuple[Path, Path, float]], duration: float,
    ) -> None:
        """Move the parts into place and swap them for the source's episodes in one step (NF-8)."""
        now = self.clock.now()
        old = library.list_source_episodes(self.conn, source.id)
        titles = splitting.kept_titles(proposal.segments, source.title)
        delete_source = proposal.delete_source
        show_id = old[0].show_id if old else source.show_id
        # A-22: the old episode of an awaiting compilation was hidden only for this review, so the parts show.
        hidden = source.publish == "hold" or (bool(old) and all(e.hidden for e in old) and not source.awaiting_split)
        rel_dir = Path(SHOWS_DIR) / str(show_id)
        written: list[Path] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            (self.media_dir / rel_dir).mkdir(parents=True, exist_ok=True)
            new_ids: list[int] = []
            for n, (seg, title, (video, thumb, length)) in enumerate(zip(kept, titles, parts), 1):
                rel_video = rel_dir / splitting.episode_file_name(source.youtube_id, job.id, n)
                os.chmod(video, FILE_MODE)
                os.replace(video, self.media_dir / rel_video)
                written.append(self.media_dir / rel_video)
                episode_id = library.add_episode(
                    self.conn, show_id, title, str(rel_video), now=now, duration_s=length,
                    source_video_id=source.id, hidden=hidden,
                )
                self.conn.execute("UPDATE episode SET start_s = ?, end_s = ? WHERE id = ?",
                                  (seg.start_s, min(seg.end_s, duration), episode_id))
                rel_thumb = images.save_episode_thumbnail(self.media_dir, episode_id, thumb.read_bytes())
                written.append(self.media_dir / rel_thumb)
                self.conn.execute("UPDATE episode SET thumbnail_path = ? WHERE id = ?", (rel_thumb, episode_id))
                new_ids.append(episode_id)
            # The parts take the first old episode's place in the show's order.
            old_ids = {e.id for e in old}
            current = [r["id"] for r in self.conn.execute(
                "SELECT id FROM episode WHERE show_id = ? ORDER BY sort_order, id", (show_id,)) if r["id"] not in new_ids]
            first_old = next((i for i, eid in enumerate(current) if eid in old_ids), len(current))
            order = [eid for eid in current if eid not in old_ids]
            at = sum(1 for eid in current[:first_old] if eid not in old_ids)
            order[at:at] = new_ids
            for i, eid in enumerate(order):
                self.conn.execute("UPDATE episode SET sort_order = ? WHERE id = ?", (i, eid))
            for eid in old_ids:
                self.conn.execute("DELETE FROM episode WHERE id = ?", (eid,))
            if delete_source:
                self.conn.execute("UPDATE source_video SET file_path = NULL, updated_at = ? WHERE id = ?",
                                  (to_db(now), source.id))
            self.conn.execute("UPDATE source_video SET awaiting_split = 0, updated_at = ? WHERE id = ?",
                              (to_db(now), source.id))
            library.mark_split(self.conn, source.id, "done", now=now)
            jobs.complete(self.conn, job.id, now=now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            for path in written:
                path.unlink(missing_ok=True)
            raise
        candidates = [p for e in old for p in (e.file_path, e.thumbnail_path) if p]
        if delete_source and source.file_path:
            candidates.append(source.file_path)
        library._remove_unreferenced(self.conn, self.media_dir, candidates)
        log.info("job %d: split source video %d into %d episodes%s", job.id, source.id, len(kept),
                 " (hidden)" if hidden else "")

    # ------------------------------------------------------------ title-card detection (ES-4..ES-9)

    def _run_detect(self, job: Job) -> None:
        """ES-4: find the show's title cards in the source and save the cuts as a proposal to review (ES-7).

        It only reads the file, so it doesn't wait for playback, and it never cuts or approves anything.
        """
        from tellybox import detect  # OpenCV is only needed by the worker

        now = self.clock.now()

        def fail(error: str, *, retryable: bool = False) -> None:
            jobs.fail(self.conn, job.id, error, now=self.clock.now(), retryable=retryable)
            log.warning("job %d detect failed: %s", job.id, error)

        source = get_source_video(self.conn, job.target_id) if job.target_id is not None else None
        if source is None:
            return fail("source video no longer exists")
        path = self.media_dir / source.file_path if source.file_path else None
        if source.status != "ready" or path is None or not path.is_file():
            return fail("the source video file is gone")
        show_id = library.split_show_id(self.conn, source.id)
        stored = library.get_split_profile(self.conn, show_id) if show_id is not None else None
        if stored is None or not stored.usable:
            return fail("no title card marked for this show")
        profile = detect.Profile(
            references=[detect.Reference(r.card_hash, detect.Region(*r.region) if r.region else None)
                        for r in stored.references],
            match_threshold=stored.match_threshold, length_hint_s=stored.length_hint_s,
            snap_window_s=stored.snap_window_s, ocr=stored.ocr,
            ocr_region=detect.Region(*stored.ocr_region) if stored.ocr_region else None,
        )
        try:
            cuts = detect.detect(path, profile, on_progress=self._progress(job.id))
            proposal = library.get_split(self.conn, source.id)
            if proposal is None or not proposal.has_file:
                return fail("the source video file is gone")
            kept = _usable_cuts(sorted(cuts, key=lambda c: c.at_s), proposal.duration_s)
            segments = splitting.segments_from_cuts(
                proposal.duration_s, [c.at_s for c in kept], ["", *(c.title for c in kept)])
            detected = [{"at_s": c.at_s, "title_hit_s": c.title_hit_s, "confidence": c.confidence,
                         "snapped": c.snapped, "title": c.title} for c in kept]
            library.save_detected_split(self.conn, source.id, segments, detected, now=self.clock.now())
            jobs.complete(self.conn, job.id, now=self.clock.now())
            log.info("job %d: detected %d cuts in source video %d", job.id, len(kept), source.id)
        except library.SplitLocked:  # approved meanwhile: the admin's plan wins
            jobs.complete(self.conn, job.id, now=self.clock.now())
            log.info("job %d: detection result dropped, source video %d is being split", job.id, source.id)
        except library.SourceGone:
            fail("the source video file is gone")
        except splitting.SplitInvalid as exc:
            fail(str(exc))
        except MediaError as exc:
            fail(f"media: {exc}", retryable=True)
        except Exception as exc:
            log.exception("job %d failed", job.id)
            fail(f"{type(exc).__name__}: {exc}", retryable=True)

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
