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
from tellybox.web.admin.library_pages import FRAME_CACHE, IMAGE_CACHE, _media_path

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

    def pending_job(source_id: int, kind: JobType) -> dict | None:
        row = conn.execute(
            """SELECT id, status, progress FROM job WHERE type = ? AND target_id = ?
               AND status IN ('queued', 'downloading', 'processing') ORDER BY id DESC LIMIT 1""",
            (kind.value, source_id),
        ).fetchone()
        return {"id": row["id"], "status": row["status"], "progress": row["progress"]} if row else None

    def profile_block(show_id: int | None) -> dict | None:
        """ES-3: the show's marked title cards, as the editor needs them."""
        if show_id is None:
            return None
        profile = library.get_split_profile(conn, show_id)
        return {
            "usable": profile.usable, "length_hint_s": profile.length_hint_s, "auto_detect": profile.auto_detect,
            "references": [{"id": r.id, "image_url": f"/admin/img/split-reference/{r.id}.jpg", "region": r.region}
                           for r in profile.references],
        }

    def split_state(proposal: library.SplitProposal) -> dict:
        sid = proposal.source_video_id
        row = conn.execute("SELECT file_path, awaiting_split FROM source_video WHERE id = ?", (sid,)).fetchone()
        rel, awaiting = row["file_path"], row["awaiting_split"]
        show_id = library.split_show_id(conn, sid)
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
            "job": pending_job(sid, JobType.SPLIT),
            "show_id": show_id, "profile": profile_block(show_id),
            "detected": library.get_detected(conn, sid),
            "awaiting_split": bool(awaiting),
            "detect_job": pending_job(sid, JobType.DETECT),
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


    # --------------------------------------------------------------------- title cards and detection (v6)

    @router.post("/admin/api/splits/{source_id}/reference")
    async def add_reference(request: Request, source_id: int) -> JSONResponse:
        """ES-3: keep the frame at `at_s` (with an optional region of it) as a title card of the show."""
        try:
            body = await request.json()
            at_s = float(body["at_s"])
            region = body.get("region")
            if region is not None and (not isinstance(region, list) or len(region) != 4):
                raise TypeError
            if region is not None:
                region = [float(v) for v in region]
            if not 0 <= at_s < 1e7:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": _("That title card can't be used.")}, status_code=422)
        row = conn.execute("SELECT file_path FROM source_video WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        path = _media_path(media_dir, row["file_path"])
        show_id = library.split_show_id(conn, source_id)
        if path is None or show_id is None:
            return JSONResponse({"error": _("The original video isn't available, so it can't be split.")},
                                status_code=409)
        try:
            image = grab_frame(path, at_s)
            library.add_split_reference(conn, media_dir, show_id, image, region, source_video_id=source_id,
                                        at_s=at_s, now=ctx.clock.now())
        except (ImageError, ValueError):
            return JSONResponse({"error": _("That title card can't be used.")}, status_code=422)
        return JSONResponse(profile_block(show_id), headers=NO_STORE)

    @router.post("/admin/api/splits/{source_id}/detect")
    def detect(source_id: int) -> JSONResponse:
        """ES-4: queue the search for title cards."""
        try:
            job_id = library.request_detect(conn, source_id, now=ctx.clock.now())
        except KeyError:
            raise HTTPException(404) from None
        except library.SourceGone:
            return JSONResponse({"error": _("The original video isn't available, so it can't be split.")},
                                status_code=409)
        except LookupError:
            return JSONResponse({"error": _("Mark a title card first.")}, status_code=409)
        except library.SplitLocked:
            return JSONResponse({"error": _("The video is being cut, so the plan can't change now.")},
                                status_code=409)
        return JSONResponse({"job_id": job_id}, headers=NO_STORE)

    @router.get("/admin/img/split-reference/{reference_id}.jpg")
    def reference_image(reference_id: int) -> Response:
        row = conn.execute("SELECT image_path FROM split_reference WHERE id = ?", (reference_id,)).fetchone()
        path = _media_path(media_dir, row["image_path"]) if row else None
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, headers=IMAGE_CACHE)

    @router.post("/admin/split-references/{reference_id}/delete")
    def delete_reference(reference_id: int) -> Response:
        row = conn.execute("SELECT show_id FROM split_reference WHERE id = ?", (reference_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        library.delete_split_reference(conn, media_dir, reference_id)
        return see_other(f"/admin/shows/{row['show_id']}#splitting", flash=_("Title card removed."))

    @router.post("/admin/sources/{source_id}/publish-whole")
    def publish_whole(source_id: int) -> Response:
        """ES-10, A-22: keep a held compilation as one video and show it to the kids."""
        try:
            library.publish_unsplit(conn, source_id, now=ctx.clock.now())
        except KeyError:
            raise HTTPException(404) from None
        episodes = library.list_source_episodes(conn, source_id)
        target = f"/admin/episodes/{episodes[0].id}" if episodes else "/admin/library"
        return see_other(target, flash=_("Published to the kid app."))

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
