"""Manual episode splitting in the admin (ES-1, ES-2, ES-7): the split page, the scrub video and
frames of the source file, and the JSON API the editor autosaves through.

The editor itself is split.js; the page holds the plan's state as JSON (`split_state`) and the
API serves the same shape. Everything that changes the library goes through `library`.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from tellybox import library, media_format, splitting
from tellybox.i18n import _
from tellybox.images import ImageError, grab_frame
from tellybox.jobs import JobType
from tellybox.web.admin.common import AdminContext, render, see_other
from tellybox.web.admin.library_pages import FRAME_CACHE, _media_path

DEFAULT_FPS = 25.0
NO_STORE = {"Cache-Control": "no-store"}


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn = ctx.conn
    media_dir = ctx.config.media_dir
    fps_cache: dict[tuple[int, str], float] = {}  # probed once per source file, so autosave stays cheap
    fps_lock = threading.Lock()

    def source_file(source_id: int) -> Path:
        row = conn.execute("SELECT file_path FROM source_video WHERE id = ?", (source_id,)).fetchone()
        path = _media_path(media_dir, row["file_path"]) if row else None
        if path is None:
            raise HTTPException(404)
        return path

    def fps_of(source_id: int, rel: str | None) -> float:
        path = _media_path(media_dir, rel)
        if path is None:
            return DEFAULT_FPS
        key = (source_id, str(path))
        with fps_lock:
            if key in fps_cache:
                return fps_cache[key]
        try:
            fps = media_format.probe(path).fps or DEFAULT_FPS
        except media_format.MediaError:
            fps = DEFAULT_FPS
        with fps_lock:
            fps_cache[key] = fps
        return fps

    def split_job(source_id: int) -> dict | None:
        row = conn.execute(
            """SELECT id, status, progress FROM job WHERE type = ? AND target_id = ?
               AND status IN ('queued', 'downloading', 'processing') ORDER BY id DESC LIMIT 1""",
            (JobType.SPLIT.value, source_id),
        ).fetchone()
        return {"id": row["id"], "status": row["status"], "progress": row["progress"]} if row else None

    def split_state(proposal: library.SplitProposal) -> dict:
        sid = proposal.source_video_id
        rel = conn.execute("SELECT file_path FROM source_video WHERE id = ?", (sid,)).fetchone()["file_path"]
        return {
            "source_id": sid, "title": proposal.title, "duration_s": proposal.duration_s,
            "fps": fps_of(sid, rel),
            "video_url": f"/admin/sources/{sid}/video.mp4", "frame_url": f"/admin/sources/{sid}/frame.jpg",
            "status": proposal.status, "origin": proposal.origin, "stored": proposal.stored,
            "editable": proposal.editable, "has_file": proposal.has_file,
            "delete_source": proposal.delete_source, "error": proposal.error,
            "updated_at": proposal.updated_at.isoformat() if proposal.updated_at else None,
            "segments": [{"start_s": s.start_s, "end_s": s.end_s, "title": s.title, "keep": s.keep}
                         for s in proposal.segments],
            "chapters": proposal.chapters,
            "job": split_job(sid),
        }

    def page_context(proposal: library.SplitProposal, **extra) -> dict:
        return {
            "nav": "library", "proposal": proposal, "state": split_state(proposal),
            "episodes": library.list_source_episodes(conn, proposal.source_video_id),
            "kept": sum(1 for s in proposal.segments if s.keep),
            "can_discard": proposal.stored and proposal.status in ("draft", "review", "failed"),
            "waiting": proposal.status in ("approved", "cutting"), **extra,
        }

    def proposal_or_404(source_id: int) -> library.SplitProposal:
        proposal = library.get_split(conn, source_id)
        if proposal is None:
            raise HTTPException(404)
        return proposal

    # --------------------------------------------------------------------- source file

    @router.get("/admin/sources/{source_id}/video.mp4")
    def source_video(source_id: int) -> Response:
        return FileResponse(source_file(source_id), media_type="video/mp4", headers=NO_STORE)

    @router.get("/admin/sources/{source_id}/frame.jpg")
    def source_frame(source_id: int, t: float = 0.0) -> Response:
        path = source_file(source_id)
        try:
            image = grab_frame(path, t)
        except ImageError as e:
            raise HTTPException(422, str(e)) from e
        return Response(content=image, media_type="image/jpeg", headers=FRAME_CACHE)

    # --------------------------------------------------------------------- API

    @router.get("/admin/api/splits/{source_id}")
    def get_state(source_id: int) -> JSONResponse:
        return JSONResponse(split_state(proposal_or_404(source_id)), headers=NO_STORE)

    @router.put("/admin/api/splits/{source_id}")
    async def put_state(request: Request, source_id: int) -> JSONResponse:
        try:
            body = await request.json()
            raw = body["segments"]
            if not isinstance(raw, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": _("The plan is malformed.")}, status_code=422)
        origin = body.get("origin")
        try:
            segments = [splitting.from_dict(d) for d in raw]
            if origin is not None and origin not in splitting.ORIGINS:
                raise splitting.SplitInvalid(_("The plan is malformed."))
            proposal = library.save_split(conn, source_id, segments, now=ctx.clock.now(), origin=origin)
        except splitting.SplitInvalid as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        except library.SplitLocked:
            return JSONResponse({"error": _("The video is being cut, so the plan can't change now.")},
                                status_code=409)
        except library.SourceGone:
            return JSONResponse({"error": _("The original video isn't available, so it can't be split.")},
                                status_code=409)
        except KeyError:
            raise HTTPException(404) from None
        return JSONResponse(split_state(proposal), headers=NO_STORE)

    # --------------------------------------------------------------------- page

    @router.get("/admin/sources/{source_id}/split")
    def split_page(request: Request, source_id: int) -> Response:
        return render(request, "split.html", **page_context(proposal_or_404(source_id)))

    @router.post("/admin/sources/{source_id}/split/approve")
    def approve(request: Request, source_id: int, delete_source: str = Form("")) -> Response:
        proposal = proposal_or_404(source_id)
        try:
            library.approve_split(conn, source_id, delete_source=bool(delete_source), now=ctx.clock.now())
        except KeyError:
            error = _("Save a plan first.")
        except splitting.SplitInvalid as e:
            error = str(e)
        except library.SplitLocked:
            error = _("The video is being cut, so the plan can't change now.")
        except library.SourceGone:
            error = _("The original video isn't available, so it can't be split.")
        else:
            return see_other(f"/admin/sources/{source_id}/split",
                             flash=_("Splitting is queued. The parts appear when it's done."))
        return render(request, "split.html", 422, **page_context(proposal, error=error))

    @router.post("/admin/sources/{source_id}/split/discard")
    def discard(request: Request, source_id: int) -> Response:
        proposal = proposal_or_404(source_id)
        try:
            library.discard_split(conn, source_id)
        except library.SplitLocked:
            return render(request, "split.html", 422, **page_context(
                proposal, error=_("The video is being cut, so the plan can't change now.")))
        episodes = library.list_source_episodes(conn, source_id)
        target = f"/admin/episodes/{episodes[0].id}" if episodes else "/admin/library"
        return see_other(target, flash=_("Split plan discarded."))

    return router
