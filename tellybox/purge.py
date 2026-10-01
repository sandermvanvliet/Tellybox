"""History and job purge (AD-5): deletes only closed rows well in the past.

Run from the worker's daily slot (tellybox.worker). The receiver event log (CR-6) goes with the
history. This only ever deletes watch_session rows that already ended, and daily_usage/override_log rows for
days long gone; an open session (ended_at IS NULL) and today's rows are never
touched. Those three tables are otherwise owned by the cast service (CLAUDE.md:
"only the cast service writes timer, history and position tables"), so this
can't race it as long as that invariant holds.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from tellybox.db import to_db
from tellybox.timer import day_for
from tellybox import auth

log = logging.getLogger(__name__)

HISTORY_RETENTION_DAYS = 21  # AD-5: watch history and the day-scoped timer tables
JOB_RETENTION_DAYS = 30  # finished download/update jobs
THROTTLE_IDLE_DAYS = 1  # login_throttle rows not touched in this long


def purge(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo, reset_time: time) -> dict[str, int]:
    """Delete closed history, old jobs and stale auth rows. Returns the row count per kind."""
    watch_cutoff = to_db(now - timedelta(days=HISTORY_RETENTION_DAYS))
    today = day_for(now, reset_time, tz)
    day_cutoff = (today - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    job_cutoff = to_db(now - timedelta(days=JOB_RETENTION_DAYS))
    throttle_cutoff = to_db(now - timedelta(days=THROTTLE_IDLE_DAYS))

    counts = {
        # watch_session_profile rows cascade (ON DELETE CASCADE); an open session is never touched.
        "watch_session": conn.execute(
            "DELETE FROM watch_session WHERE ended_at IS NOT NULL AND ended_at < ?", (watch_cutoff,)
        ).rowcount,
        "override_log": conn.execute("DELETE FROM override_log WHERE day < ?", (day_cutoff,)).rowcount,
        "daily_usage": conn.execute("DELETE FROM daily_usage WHERE day < ?", (day_cutoff,)).rowcount,
        "job": conn.execute(
            "DELETE FROM job WHERE finished_at IS NOT NULL AND status IN ('ready', 'failed') AND finished_at < ?",
            (job_cutoff,),
        ).rowcount,
        "receiver_event": conn.execute("DELETE FROM receiver_event WHERE at < ?", (watch_cutoff,)).rowcount,
        "admin_session": auth.purge_expired_sessions(conn, now),
        "login_throttle": conn.execute(
            "DELETE FROM login_throttle WHERE updated_at < ?", (throttle_cutoff,)
        ).rowcount,
    }
    log.info("purge: %s", counts)
    return counts
