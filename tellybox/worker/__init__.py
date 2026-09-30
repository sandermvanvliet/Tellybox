"""Background job worker: downloads, conversions, SponsorBlock re-checks, yt-dlp updates (CI-3, CI-5, SB-3)."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tellybox import ingest, jobs
from tellybox import purge as purge_module
from tellybox.clock import Clock
from tellybox.db import from_db, to_db
from tellybox.ingest import JobRunner
from tellybox.jobs import JobType
from tellybox.timer import day_for

log = logging.getLogger(__name__)

POLL_S = 2.0
UPDATE_AT = time(3, 0)  # local; before the 04:00 allowance reset


def update_due(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> bool:
    """A yt-dlp update is due once a day after UPDATE_AT, and right away if none ever ran (CI-5).

    Based on the last finished update job (ready or finally failed), not on the recorded
    version, which the worker also refreshes at startup.
    """
    last = conn.execute(
        "SELECT max(finished_at) FROM job WHERE type = 'update_ytdlp' AND finished_at IS NOT NULL"
    ).fetchone()[0]
    if last is None:
        return True
    local_now = now.astimezone(tz)
    today_slot = datetime.combine(local_now.date(), UPDATE_AT, tz)
    last_slot = today_slot if local_now >= today_slot else today_slot - timedelta(days=1)
    return from_db(last).astimezone(tz) < last_slot


def _reset_time(conn: sqlite3.Connection) -> time:
    row = conn.execute("SELECT reset_time FROM settings WHERE id = 1").fetchone()
    return time.fromisoformat(row["reset_time"])


class Worker:
    def __init__(self, runner: JobRunner, tz: ZoneInfo, *, auto_update: bool = True, auto_purge: bool = True,
                 auto_recheck: bool = True) -> None:
        self.runner = runner
        self.tz = tz
        self.auto_update = auto_update
        self.auto_purge = auto_purge
        self.auto_recheck = auto_recheck
        self.stop = threading.Event()
        self._last_purge_day: date | None = None  # in-memory: at most one purge per slot-day
        self._last_recheck_day: date | None = None  # likewise for the SponsorBlock re-checks

    @property
    def conn(self) -> sqlite3.Connection:
        return self.runner.conn

    @property
    def clock(self) -> Clock:
        return self.runner.clock

    def step(self) -> bool:
        """Run at most one job. Returns True if a job ran."""
        now = self.clock.now()
        if self.auto_update and update_due(self.conn, now, self.tz):
            ingest.request_ytdlp_update(self.conn, now=now)
        if self.auto_purge and self._purge_due(now):
            self._run_purge(now)
        if self.auto_recheck and self._recheck_due(now):
            self._enqueue_rechecks(now)
        job = jobs.claim_next(self.conn, now=now)
        if job is None:
            return False
        log.info("job %d (%s) started, attempt %d", job.id, job.type, job.attempts)
        self.runner.run(job)
        return True

    def _purge_due(self, now: datetime) -> bool:
        """AD-5: once per 03:00-to-03:00 day, and at startup (purging twice is harmless)."""
        return self._last_purge_day != day_for(now, UPDATE_AT, self.tz)

    def _run_purge(self, now: datetime) -> None:
        # Mark the day first: a failing purge is logged once a day, not on every poll.
        self._last_purge_day = day_for(now, UPDATE_AT, self.tz)
        purge_module.purge(self.conn, now, self.tz, _reset_time(self.conn))

    def _recheck_due(self, now: datetime) -> bool:
        """SB-3: once per 03:00-to-03:00 day, and at startup."""
        return self._last_recheck_day != day_for(now, UPDATE_AT, self.tz)

    def _enqueue_rechecks(self, now: datetime) -> None:
        """SB-3: one re-check per published video still inside its window, unless the admin turned SponsorBlock off."""
        self._last_recheck_day = day_for(now, UPDATE_AT, self.tz)  # marked first, like the purge
        rows = self.conn.execute(
            """SELECT id FROM source_video
               WHERE status = 'ready' AND sb_recheck_until > ? AND COALESCE(sb_status, '') != 'admin_off'
               ORDER BY id""",
            (to_db(now),),
        ).fetchall()
        queued = 0
        for row in rows:
            if not jobs.has_pending_for(self.conn, row["id"], (JobType.SB_RECHECK, JobType.REDOWNLOAD)):
                jobs.enqueue(self.conn, JobType.SB_RECHECK, row["id"], now=now, max_attempts=2)
                queued += 1
        if queued:
            log.info("queued %d SponsorBlock re-checks", queued)

    def run_forever(self) -> None:
        self.runner.recover()
        try:
            ingest.record_tool_version(self.conn, "yt-dlp", self.runner.ytdlp.version(),
                                       self.runner.ytdlp.active_dir(), now=self.clock.now())
        except Exception:
            log.exception("could not determine the yt-dlp version")
        log.info("worker ready")
        while not self.stop.is_set():
            try:
                if not self.step():
                    self.stop.wait(POLL_S)
            except Exception:
                log.exception("worker step failed")
                self.stop.wait(POLL_S)
