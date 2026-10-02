"""Dashboard `/admin` (AD-3, WT-7, NF-11): what's on the TV, today's time, overrides,
failed jobs and active downloads, yt-dlp version and disk usage. Live via SSE.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from tellybox.jobs import Job, JobStatus, list_jobs
from tellybox.i18n import N_, _
from tellybox.library import disk_usage
from tellybox.store import effective_allowance_s
from tellybox.web.admin.common import AdminContext, render, see_other
from tellybox.web.cast_client import CastUnavailable
from tellybox.web.overrides import apply_override

SSE_KEEPALIVE_S = 15.0
# The cast service's 422 texts for a refused override (tellybox.cast.controller), flashed as-is;
# listed so they get translated.
OVERRIDE_ERRORS = (N_("extra_minutes needs a positive value"), N_("extra_minutes is at most 240"))  # overrides.py
_UNREACHABLE = object()  # sentinel: the relay's events() stream ended or the cast service is down


def _now_playing_context(conn: sqlite3.Connection, now_playing: dict | None) -> dict | None:
    if now_playing is None:
        return None
    row = conn.execute(
        "SELECT s.name FROM episode e JOIN show s ON s.id = e.show_id WHERE e.id = ?", (now_playing["episode_id"],)
    ).fetchone()
    watchers = set(now_playing.get("profile_ids") or [])
    names = [r["name"] for r in conn.execute("SELECT id, name FROM profile ORDER BY sort_order, id") if r["id"] in watchers]
    return {**now_playing, "show_name": row["name"] if row else None, "watcher_names": names}


def _profiles_context(conn: sqlite3.Connection, state: dict | None) -> list[dict]:
    """Dashboard profile context, resolving per-profile allowance limits (A-23).

    unlimited=True when either today's override or policy says unlimited.
    """
    rows = conn.execute(
        "SELECT id, name, avatar, picture_path FROM profile ORDER BY sort_order, id"
    ).fetchall()
    watchers = set(((state or {}).get("now_playing") or {}).get("profile_ids") or [])
    timers = {p["profile_id"]: p for p in ((state or {}).get("timer") or {}).get("profiles", [])}
    result = []
    for r in rows:
        t = timers.get(r["id"])
        # Resolve allowance per profile (A-23)
        allowance_s = effective_allowance_s(conn, r["id"])
        used_s = t["used_s"] if t else 0.0
        extra_s = t["extra_s"] if t else 0.0
        policy_unlimited = allowance_s is None
        today_unlimited = bool(t["unlimited"]) if t else False
        unlimited = policy_unlimited or today_unlimited
        blocked = bool(t["blocked"]) if t else False
        remaining_s = None if unlimited else (max(0.0, allowance_s + extra_s - used_s) if allowance_s is not None else None)
        result.append({
            "id": r["id"], "name": r["name"], "avatar": r["avatar"], "picture_path": r["picture_path"],
            "watching": r["id"] in watchers, "allowance_min": None if policy_unlimited else (int(allowance_s / 60.0) if allowance_s else 0),
            "used_s": used_s, "remaining_s": remaining_s, "unlimited": unlimited, "blocked": blocked,
        })
    return result


def _jobs_context(conn: sqlite3.Connection) -> dict[str, list[Job]]:
    return {
        "failed": list_jobs(conn, statuses=[JobStatus.FAILED], limit=10),
        "active": list_jobs(conn, statuses=[JobStatus.DOWNLOADING, JobStatus.PROCESSING], limit=10),
    }


def _job_json(j: Job) -> dict:
    return {
        "id": j.id, "type": j.type.value, "target_id": j.target_id, "status": j.status.value,
        "progress": j.progress, "error": j.error,
    }


def _ytdlp_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT version FROM tool_version WHERE name = 'yt-dlp'").fetchone()
    return row["version"] if row else None


def _disk_context(conn: sqlite3.Connection, media_dir: Path) -> dict:
    media_bytes = disk_usage(conn, media_dir).total_bytes
    try:
        free_bytes = shutil.disk_usage(media_dir).free
    except OSError:
        free_bytes = None
    return {"media_bytes": media_bytes, "free_bytes": free_bytes}


def _receiver_context(ctx: AdminContext, state: dict | None) -> dict | None:
    """The TV receiver line (CR-6): what the next pick uses; None if the cast service doesn't say."""
    receiver = (state or {}).get("receiver")
    if not receiver:
        return None
    result = {"kind": receiver.get("kind"), "until": None, "reason": receiver.get("last_error") or "",
              "problem": None, "refused": False}
    until = receiver.get("fallback_until")
    if until:
        deadline = datetime.fromisoformat(until)
        if deadline > ctx.clock.now():  # an expired fallback is only stale state
            result["until"] = deadline.astimezone(ctx.config.tz).strftime("%H:%M")
            result["refused"] = bool(receiver.get("refused"))
    last = receiver.get("last_failure")
    if last and receiver.get("failures_24h"):  # an old problem is no news, and the time shown has no date
        at = datetime.fromisoformat(last["at"]).astimezone(ctx.config.tz).strftime("%H:%M")
        result["problem"] = {"kind": last["kind"], "detail": last.get("detail") or "", "time": at}
    return result


def _page_context(ctx: AdminContext, state: dict | None, unreachable: bool) -> dict:
    timer = (state or {}).get("timer")
    return {
        "unreachable": unreachable,
        "connection": (state or {}).get("connection"),
        "device": (state or {}).get("device"),
        "now_playing": _now_playing_context(ctx.conn, (state or {}).get("now_playing")),
        "timer": timer,
        "time_up": bool((state or {}).get("time_up")),
        "receiver": _receiver_context(ctx, state),
        "profiles": _profiles_context(ctx.conn, state),
        "jobs": _jobs_context(ctx.conn),
        "ytdlp_version": _ytdlp_version(ctx.conn),
        "disk": _disk_context(ctx.conn, ctx.config.media_dir),
        "version": ctx.config.version,
    }


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()

    @router.get("/admin")
    async def dashboard(request: Request) -> HTMLResponse:
        try:
            state = await ctx.cast.state()
            unreachable = False
        except CastUnavailable:
            state, unreachable = None, True
        return render(request, "dashboard.html", nav="dashboard", **_page_context(ctx, state, unreachable))

    @router.get("/admin/api/dashboard")
    def dashboard_api() -> JSONResponse:
        """Polled every 5 s by dashboard.js for the parts that don't come over SSE."""
        jobs = _jobs_context(ctx.conn)
        return JSONResponse({
            "jobs": {"failed": [_job_json(j) for j in jobs["failed"]], "active": [_job_json(j) for j in jobs["active"]]},
            "ytdlp_version": _ytdlp_version(ctx.conn),
            "disk": _disk_context(ctx.conn, ctx.config.media_dir),
        })

    @router.post("/admin/stop")
    async def stop(request: Request) -> Response:
        try:
            await ctx.cast.stop()
        except CastUnavailable:
            return see_other("/admin", flash=_("Cast service unreachable; nothing was stopped."))
        return see_other("/admin", flash=_("Stopped."))

    @router.post("/admin/overrides")
    async def overrides(
        request: Request,
        kind: str = Form(...),
        value: int | None = Form(None),
        profile_id: int | None = Form(None),
    ) -> Response:
        try:
            await apply_override(ctx.cast, kind, value, None if profile_id is None else [profile_id])
        except ValueError as exc:
            return see_other("/admin", flash=_(str(exc)))  # our own cast service's text: see OVERRIDE_ERRORS
        except CastUnavailable:
            return see_other("/admin", flash=_("Cast service unreachable; the change wasn't applied."))
        return see_other("/admin", flash=_("Done."))

    @router.get("/admin/events")
    async def events() -> StreamingResponse:
        """SSE relay of `ctx.cast.events()`, with our own keepalive so a quiet TV doesn't look dead."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def relay() -> None:
            try:
                async for state in ctx.cast.events():
                    if queue.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                    queue.put_nowait(state)
            except CastUnavailable:
                pass
            finally:
                # Blocking put: wait for room rather than risk QueueFull dropping the sentinel silently.
                with contextlib.suppress(asyncio.CancelledError):
                    await queue.put(_UNREACHABLE)

        task = asyncio.create_task(relay(), name="admin-events-relay")

        async def stream():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is _UNREACHABLE:
                        yield f"data: {json.dumps({'connection': 'unreachable'})}\n\n"
                        return
                    yield f"data: {json.dumps(item)}\n\n"
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return router
