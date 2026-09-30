"""Jobs (CI-3, CI-5): queue status, retry and the yt-dlp update button."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from tellybox import ingest, jobs
from tellybox.i18n import _
from tellybox.jobs import Job, JobStatus, JobType
from tellybox.web.admin.common import AdminContext, render, see_other

JOB_LIMIT = 50
_BADGE = {
    JobStatus.READY: "ok",
    JobStatus.FAILED: "bad",
    JobStatus.DOWNLOADING: "warn",
    JobStatus.PROCESSING: "warn",
}


@dataclass(frozen=True)
class JobRow:
    """A job plus the presentation bits the template and the JSON API both need."""

    job: Job
    title: str | None  # jobs of a video: download, sb_recheck and redownload
    badge: str  # admin.css .badge modifier, or "" for the plain badge


def _title(conn: sqlite3.Connection, job: Job) -> str | None:
    if job.type not in (JobType.DOWNLOAD, JobType.SB_RECHECK, JobType.REDOWNLOAD) or job.target_id is None:
        return None
    row = conn.execute("SELECT title FROM source_video WHERE id = ?", (job.target_id,)).fetchone()
    return row["title"] if row else None


def _label(row: JobRow) -> str:
    if row.title:
        return row.title
    labels = {JobType.UPDATE_YTDLP: _("Update yt-dlp"), JobType.SB_RECHECK: _("Check SponsorBlock"),
              JobType.REDOWNLOAD: _("Download again")}
    return labels.get(row.job.type, row.job.type.value)


def _rows(conn: sqlite3.Connection) -> list[JobRow]:
    return [JobRow(job=j, title=_title(conn, j), badge=_BADGE.get(j.status, "")) for j in jobs.list_jobs(conn, limit=JOB_LIMIT)]


def _as_json(row: JobRow) -> dict:
    j = row.job
    return {
        "id": j.id,
        "label": _label(row),
        "status": j.status.value,
        "badge": row.badge,
        "progress": j.progress,
        "attempts": j.attempts,
        "max_attempts": j.max_attempts,
        "error": j.error,
        "retryable": j.status == JobStatus.FAILED,
    }


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn = ctx.conn

    def ytdlp_version() -> str | None:
        row = conn.execute("SELECT version FROM tool_version WHERE name = 'yt-dlp'").fetchone()
        return row["version"] if row else None

    @router.get("/admin/jobs")
    def jobs_page(request: Request):
        return render(request, "jobs.html", nav="jobs", tz=ctx.config.tz, rows=_rows(conn),
                     ytdlp_version=ytdlp_version(), update_pending=jobs.has_pending(conn, JobType.UPDATE_YTDLP))

    @router.get("/admin/api/jobs")
    def jobs_api() -> JSONResponse:
        return JSONResponse([_as_json(r) for r in _rows(conn)])

    @router.post("/admin/jobs/{job_id}/retry")
    def retry(job_id: int):
        try:
            ingest.retry(conn, job_id, now=ctx.clock.now())
        except KeyError:
            raise HTTPException(404, "job not found") from None
        except ValueError as exc:
            return see_other("/admin/jobs", flash=str(exc))
        return see_other("/admin/jobs", flash=_("Job %(id)d queued for retry") % {"id": job_id})

    @router.post("/admin/jobs/update-ytdlp")
    def update_ytdlp():
        job_id = ingest.request_ytdlp_update(conn, now=ctx.clock.now())
        flash = _("yt-dlp update queued") if job_id is not None else _("An update is already pending")
        return see_other("/admin/jobs", flash=flash)

    return router
