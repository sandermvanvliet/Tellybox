"""Background job queue in SQLite (CI-3, NF-7).

The web process enqueues and reads jobs; the single worker claims and runs them.
Only claim_next opens its own transaction (BEGIN IMMEDIATE, so two workers could
never claim the same job); every other function is a single statement or a
read followed by one write, so callers may wrap them in their own transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tellybox.db import from_db, to_db
from tellybox.i18n import _


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class JobType(StrEnum):
    DOWNLOAD = "download"
    UPDATE_YTDLP = "update_ytdlp"


RUNNING = (JobStatus.DOWNLOADING, JobStatus.PROCESSING)
BACKOFF_S = (60, 300, 1800)  # waits before automatic retries 1, 2, 3
STALE_AFTER_S = 60  # NF-7: a running job without a heartbeat this long is orphaned


@dataclass(frozen=True)
class Job:
    id: int
    type: JobType
    target_id: int | None  # source_video.id for download jobs
    status: JobStatus
    progress: float | None  # 0..1 within the current status
    error: str | None  # last error; kept visible while retrying, cleared on success
    attempts: int
    max_attempts: int
    run_after: datetime
    heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


_COLS = (
    "id, type, target_id, status, progress, error, attempts, max_attempts, "
    "run_after, heartbeat_at, created_at, updated_at, finished_at"
)
_RUNNING_SQL = ", ".join(f"'{s}'" for s in RUNNING)


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        type=JobType(row["type"]),
        target_id=row["target_id"],
        status=JobStatus(row["status"]),
        progress=row["progress"],
        error=row["error"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_after=from_db(row["run_after"]),
        heartbeat_at=from_db(row["heartbeat_at"]),
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        finished_at=from_db(row["finished_at"]),
    )


def _require(conn: sqlite3.Connection, job_id: int) -> Job:
    job = get(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    return job


def enqueue(
    conn: sqlite3.Connection, type: JobType, target_id: int | None, *, now: datetime, max_attempts: int = 4
) -> int:
    ts = to_db(now)
    cur = conn.execute(
        """INSERT INTO job (type, target_id, status, max_attempts, run_after, created_at, updated_at)
           VALUES (?, ?, 'queued', ?, ?, ?, ?)""",
        (JobType(type).value, target_id, max_attempts, ts, ts, ts),
    )
    return cur.lastrowid


def claim_next(conn: sqlite3.Connection, *, now: datetime) -> Job | None:
    """Take the oldest due queued job and mark it running."""
    ts = to_db(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id, type FROM job WHERE status = 'queued' AND run_after <= ? ORDER BY run_after, id LIMIT 1",
            (ts,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        status = JobStatus.DOWNLOADING if row["type"] == JobType.DOWNLOAD else JobStatus.PROCESSING
        claimed = conn.execute(
            f"""UPDATE job SET status = ?, attempts = attempts + 1, progress = 0, heartbeat_at = ?, updated_at = ?
                WHERE id = ? RETURNING {_COLS}""",
            (status.value, ts, ts, row["id"]),
        ).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return _job(claimed)


def set_status(
    conn: sqlite3.Connection, job_id: int, status: JobStatus, *, now: datetime, progress: float | None = 0.0
) -> None:
    ts = to_db(now)
    conn.execute(
        "UPDATE job SET status = ?, progress = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?",
        (JobStatus(status).value, progress, ts, ts, job_id),
    )


def heartbeat(conn: sqlite3.Connection, job_id: int, *, now: datetime, progress: float | None = None) -> None:
    ts = to_db(now)
    conn.execute(
        "UPDATE job SET progress = COALESCE(?, progress), heartbeat_at = ?, updated_at = ? WHERE id = ?",
        (progress, ts, ts, job_id),
    )


def complete(conn: sqlite3.Connection, job_id: int, *, now: datetime) -> None:
    ts = to_db(now)
    conn.execute(
        """UPDATE job SET status = 'ready', progress = 1, error = NULL, updated_at = ?, finished_at = ?
           WHERE id = ?""",
        (ts, ts, job_id),
    )


def fail(conn: sqlite3.Connection, job_id: int, error: str, *, now: datetime, retryable: bool) -> Job:
    """Record a failed attempt: back off and requeue, or give up (CI-3, NF-8)."""
    job = _require(conn, job_id)
    ts = to_db(now)
    if retryable and job.attempts < job.max_attempts:
        wait = BACKOFF_S[min(max(job.attempts, 1) - 1, len(BACKOFF_S) - 1)]
        row = conn.execute(
            f"""UPDATE job SET status = 'queued', error = ?, progress = NULL, heartbeat_at = NULL,
                    run_after = ?, updated_at = ?, finished_at = NULL
                WHERE id = ? RETURNING {_COLS}""",
            (error, to_db(now + timedelta(seconds=wait)), ts, job_id),
        ).fetchone()
    else:
        row = conn.execute(
            f"""UPDATE job SET status = 'failed', error = ?, heartbeat_at = NULL, updated_at = ?, finished_at = ?
                WHERE id = ? RETURNING {_COLS}""",
            (error, ts, ts, job_id),
        ).fetchone()
    return _job(row)


def retry(conn: sqlite3.Connection, job_id: int, *, now: datetime) -> Job:
    """Admin retry (CI-3): only a failed job, with a fresh set of attempts."""
    ts = to_db(now)
    row = conn.execute(
        f"""UPDATE job SET status = 'queued', attempts = 0, progress = NULL, run_after = ?, updated_at = ?,
                finished_at = NULL
            WHERE id = ? AND status = 'failed' RETURNING {_COLS}""",
        (ts, ts, job_id),
    ).fetchone()
    if row is None:
        job = _require(conn, job_id)
        raise ValueError(
            _("job %(id)d is %(status)s, only failed jobs can be retried") % {"id": job_id, "status": job.status}
        )
    return _job(row)


def recover_stale(
    conn: sqlite3.Connection, *, now: datetime, stale_after_s: float = STALE_AFTER_S
) -> list[Job]:
    """Requeue running jobs whose heartbeat is stale_after_s old or older (NF-7).

    With stale_after_s=0 (worker startup) every running job is taken. A job that
    has used all its attempts fails instead, so a job that keeps killing the
    worker can't loop forever. The caller cleans up the returned jobs' temp files.
    """
    ts = to_db(now)
    rows = conn.execute(
        f"""UPDATE job SET
                status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                error = CASE WHEN attempts >= max_attempts
                             THEN COALESCE(error || ' / ', '') || 'interrupted (worker stopped)' ELSE error END,
                finished_at = CASE WHEN attempts >= max_attempts THEN ? ELSE NULL END,
                progress = NULL, heartbeat_at = NULL, run_after = ?, updated_at = ?
            WHERE status IN ({_RUNNING_SQL}) AND (heartbeat_at IS NULL OR heartbeat_at <= ?)
            RETURNING {_COLS}""",
        (ts, ts, ts, to_db(now - timedelta(seconds=stale_after_s))),
    ).fetchall()
    return sorted((_job(r) for r in rows), key=lambda j: j.id)


def get(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute(f"SELECT {_COLS} FROM job WHERE id = ?", (job_id,)).fetchone()
    return _job(row) if row else None


def list_jobs(
    conn: sqlite3.Connection, *, statuses: Iterable[JobStatus] | None = None, limit: int = 50
) -> list[Job]:
    """Newest first."""
    if statuses is None:
        rows = conn.execute(f"SELECT {_COLS} FROM job ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        wanted = [JobStatus(s).value for s in statuses]
        if not wanted:
            return []
        rows = conn.execute(
            f"SELECT {_COLS} FROM job WHERE status IN ({', '.join('?' * len(wanted))}) ORDER BY id DESC LIMIT ?",
            (*wanted, limit),
        ).fetchall()
    return [_job(r) for r in rows]


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Jobs per status; statuses without jobs are left out (admin API, HA-2)."""
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM job GROUP BY status")}


def has_pending(conn: sqlite3.Connection, type: JobType) -> bool:
    """Any queued or running job of this type (e.g. to avoid stacking yt-dlp updates)."""
    row = conn.execute(
        f"SELECT 1 FROM job WHERE type = ? AND status IN ('queued', {_RUNNING_SQL}) LIMIT 1",
        (JobType(type).value,),
    ).fetchone()
    return row is not None


def next_run_after(conn: sqlite3.Connection) -> datetime | None:
    """Earliest run_after among queued jobs, so the worker can sleep until then."""
    row = conn.execute("SELECT MIN(run_after) AS t FROM job WHERE status = 'queued'").fetchone()
    return from_db(row["t"])
