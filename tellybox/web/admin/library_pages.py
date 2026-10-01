"""Library admin pages (CI-4, CI-6, LM-1..LM-4): shows, episodes, artwork/thumbnails, held downloads.

Per-show settings in v1 are autoplay only; the splitting profile in LM-4 is v2 (see
docs/plans/step5-admin.md). Show reordering isn't requested by the PRD, so shows are
shown in their existing sort_order (set once, by ingest); only episodes get up/down.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from tellybox import ingest, jobs, library, sponsorblock
from tellybox.i18n import N_, _, ngettext
from tellybox.images import MAX_UPLOAD_BYTES, ImageError, clean_upload, grab_frame, save_episode_thumbnail, save_show_artwork
from tellybox.jobs import JobType
from tellybox.web.admin.common import AdminContext, render, see_other

PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # YouTube playlist ids (CI-7)
IMAGE_CACHE = {"Cache-Control": "no-cache"}  # revalidate (304 via ETag): replacements show at once
FRAME_CACHE = {"Cache-Control": "no-store"}


def _media_path(media_dir: Path, rel: str | None) -> Path | None:
    """Resolved path of `rel` inside media_dir, or None if missing or outside it (like kid.py's image())."""
    if not rel:
        return None
    root = media_dir.resolve()
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _parse_time(raw: str | None) -> float | None:
    """Seconds from a plain number or a [[HH:]MM:]SS(.s) string; None if blank or unparsable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if ":" in raw:
            parts = raw.split(":")
            if len(parts) > 3:
                return None
            secs = 0.0
            for part in parts:
                secs = secs * 60 + float(part)
        else:
            secs = float(raw)
    except ValueError:
        return None
    return secs if secs >= 0 else None


def _hint_text(seconds: float | None) -> str:
    """The length hint as the form shows it: minutes, or mm:ss when it isn't a whole number of minutes."""
    if seconds is None:
        return ""
    whole, secs = divmod(round(seconds), 60)
    return str(whole) if secs == 0 else f"{whole}:{secs:02d}"


def _num_text(value: float) -> str:
    return f"{value:g}"


def _profile_form(profile: library.SplitProfile) -> dict:
    """The splitting form's fields as text: the saved profile, or what was submitted."""
    region = profile.ocr_region or [None] * 4
    return {
        "threshold": str(profile.match_threshold), "length_hint": _hint_text(profile.length_hint_s),
        "snap_window": _num_text(profile.snap_window_s), "ocr": profile.ocr, "auto_detect": profile.auto_detect,
        "ocr_region": ["" if v is None else _num_text(round(v * 100, 2)) for v in region],
    }


PROFILE_FIELDS = {
    "threshold": N_("The match threshold must be a whole number from 0 to 32."),
    "match_threshold": N_("The match threshold must be a whole number from 0 to 32."),
    "length_hint": N_("The episode length must be between 0:30 and 4 hours, as minutes (11) or mm:ss (11:30)."),
    "length_hint_s": N_("The episode length must be between 0:30 and 4 hours, as minutes (11) or mm:ss (11:30)."),
    "snap_window": N_("The snap window must be from 0 to 120 seconds."),
    "snap_window_s": N_("The snap window must be from 0 to 120 seconds."),
    "region": N_("The text region needs four percentages (left, top, width, height) that fit inside the frame."),
}


def _profile_error(error: ValueError) -> str:
    key = "region" if str(error).startswith("region") else str(error)
    return _(PROFILE_FIELDS.get(key, N_("Check the splitting settings.")))


def _parse_profile_form(raw: dict) -> dict:
    """Form text to save_split_profile arguments; ValueError carries the field name."""
    try:
        threshold = int(raw["threshold"])
    except ValueError:
        raise ValueError("threshold") from None
    hint: float | None = None
    if raw["length_hint"]:
        text = raw["length_hint"]
        hint = _parse_time(text)  # mm:ss
        if hint is not None and ":" not in text:
            hint *= 60  # a plain number is minutes
        if hint is None:
            raise ValueError("length_hint")
    try:
        snap = float(raw["snap_window"])
    except ValueError:
        raise ValueError("snap_window") from None
    region_text = raw["ocr_region"]
    region: list[float] | None = None
    if any(region_text):
        try:
            region = [round(float(v) / 100, 4) for v in region_text]
        except ValueError:
            raise ValueError("region") from None
    return {"match_threshold": threshold, "length_hint_s": hint, "snap_window_s": snap, "ocr": raw["ocr"],
            "ocr_region": region, "auto_detect": raw["auto_detect"]}


def _episode_image_candidates(conn, episode_id: int) -> list[str] | None:
    """Episode thumbnail, then its source video's thumbnail; None if the episode doesn't exist."""
    row = conn.execute(
        """SELECT e.thumbnail_path, sv.thumbnail_path AS source_thumb
           FROM episode e LEFT JOIN source_video sv ON sv.id = e.source_video_id
           WHERE e.id = ?""",
        (episode_id,),
    ).fetchone()
    if row is None:
        return None
    return [p for p in (row["thumbnail_path"], row["source_thumb"]) if p]


def _show_image_candidates(conn, show_id: int) -> list[str] | None:
    """Show artwork, then every episode's thumbnail in order; None if the show doesn't exist."""
    row = conn.execute("SELECT artwork_path FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        return None
    thumbs = [r[0] for r in conn.execute(
        "SELECT thumbnail_path FROM episode WHERE show_id = ? ORDER BY sort_order, id", (show_id,)
    )]
    return [p for p in [row["artwork_path"], *thumbs] if p]


def group_held(held: list[library.HeldDownload]) -> tuple[list[library.HeldDownload], list[dict]]:
    """Split held downloads into (ungrouped, playlist groups) for the library page (CI-7).

    A group is {playlist_id, title, items, ready}, in order of first appearance; `ready` counts
    what "Publish all ready" would publish.
    """
    ungrouped: list[library.HeldDownload] = []
    groups: dict[str, dict] = {}
    for item in held:
        if not item.playlist_id:
            ungrouped.append(item)
            continue
        group = groups.setdefault(item.playlist_id, {
            "playlist_id": item.playlist_id, "title": item.playlist_title or _("Playlist"), "items": [], "ready": 0,
        })
        group["items"].append(item)
        if item.status == "ready" and item.episode_id:
            group["ready"] += 1
    return ungrouped, list(groups.values())


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn = ctx.conn
    media_dir = ctx.config.media_dir

    def library_context(**extra) -> dict:
        shows, total_bytes = library.list_shows_for_admin(conn, media_dir)
        held = library.list_held_downloads(conn)
        held_single, held_groups = group_held(held)
        return {"nav": "library", "splits": library.list_splits_for_review(conn), "shows": shows, "held": held, "held_single": held_single,
                "held_groups": held_groups, "total_bytes": total_bytes, **extra}

    def show_context(show_id: int, request: Request | None = None, **extra) -> dict | None:
        show = library.get_show(conn, show_id)
        if show is None:
            return None
        episodes = library.list_episodes(conn, show_id, include_hidden=True)
        other_shows = [s for s in library.list_shows(conn) if s.id != show_id]
        global_sb = sponsorblock.effective_categories(conn, None)
        own_sb = sponsorblock.parse_csv(show.sponsorblock_categories)
        context = {
            "nav": "library", "show": show, "episodes": episodes, "other_shows": other_shows,
            "art_preview": None, "thumb_preview": None,
            "sb_mode": "default" if show.sponsorblock_categories is None else "choose" if own_sb else "off",
            "sb_global": global_sb, "sb_checked": own_sb or global_sb, **extra,
        }
        profile = library.get_split_profile(conn, show_id)
        context["split_profile"] = profile
        context.setdefault("split_form", _profile_form(profile))
        q = request.query_params if request is not None else {}
        episode_ids = {e.id for e in episodes}
        art_secs, art_ep = _parse_time(q.get("art_t")), q.get("art_ep")
        if art_secs is not None and art_ep and art_ep.isdigit() and int(art_ep) in episode_ids:
            context["art_preview"] = {"episode_id": int(art_ep), "t": art_secs}
        thumb_secs, thumb_ep = _parse_time(q.get("thumb_t")), q.get("thumb_ep")
        if thumb_secs is not None and thumb_ep and thumb_ep.isdigit() and int(thumb_ep) in episode_ids:
            context["thumb_preview"] = {"episode_id": int(thumb_ep), "t": thumb_secs}
        return context

    def show_or_404(request: Request, show_id: int, **extra) -> dict:
        data = show_context(show_id, request, **extra)
        if data is None:
            raise HTTPException(404)
        return data

    def episode_or_404(episode_id: int) -> library.Episode:
        ep = library.get_episode(conn, episode_id)
        if ep is None:
            raise HTTPException(404)
        return ep

    # --------------------------------------------------------------------- library page

    @router.get("/admin/library")
    def library_page(request: Request) -> Response:
        return render(request, "library.html", **library_context())

    @router.post("/admin/shows")
    def add_show(request: Request, name: str = Form(...)) -> Response:
        if not name.strip():
            return render(request, "library.html", 422, error=_("Show name must not be empty."), **library_context())
        library.create_show(conn, name.strip(), now=ctx.clock.now())
        return see_other("/admin/library", flash=_("Show created."))

    @router.post("/admin/shows/merge")
    def merge_shows_route(
        request: Request, into_id: int = Form(...), from_id: int = Form(...), confirm: str = Form("")
    ) -> Response:
        if confirm != "yes":
            return render(request, "library.html", 422, error=_("Confirm the merge first."), **library_context())
        try:
            library.merge_shows(conn, into_id, from_id, now=ctx.clock.now())
        except ValueError:
            return render(request, "library.html", 422, error=_("Cannot merge a show into itself."), **library_context())
        except KeyError:
            raise HTTPException(404) from None
        return see_other("/admin/library", flash=_("Shows merged."))

    @router.post("/admin/held/{source_id}/publish")
    def publish_held_route(source_id: int) -> Response:
        try:
            library.publish_held(conn, source_id, now=ctx.clock.now())
        except KeyError:
            raise HTTPException(404) from None
        return see_other("/admin/library", flash=_("Published to the kid app."))

    @router.post("/admin/held/playlist/{playlist_id}/publish")
    def publish_held_playlist_route(playlist_id: str) -> Response:
        if not PLAYLIST_ID.fullmatch(playlist_id):
            raise HTTPException(404)
        try:
            count = library.publish_held_playlist(conn, playlist_id, now=ctx.clock.now())
        except KeyError:
            raise HTTPException(404) from None
        if count == 0:
            return see_other("/admin/library", flash=_("Nothing in that playlist is ready yet."))
        flash = ngettext("Published %(num)d video to the kid app.", "Published %(num)d videos to the kid app.", count)
        return see_other("/admin/library", flash=flash % {"num": count})

    # --------------------------------------------------------------------- show page

    @router.get("/admin/shows/{show_id}")
    def show_page(request: Request, show_id: int) -> Response:
        return render(request, "show.html", **show_or_404(request, show_id))

    @router.post("/admin/shows/{show_id}/rename")
    def rename_show_route(request: Request, show_id: int, name: str = Form(...)) -> Response:
        try:
            library.rename_show(conn, show_id, name)
        except ValueError:
            return render(request, "show.html", 422, error=_("Show name must not be empty."),
                         **show_or_404(request, show_id))
        except KeyError:
            raise HTTPException(404) from None
        return see_other(f"/admin/shows/{show_id}", flash=_("Renamed."))

    @router.post("/admin/shows/{show_id}/autoplay")
    def set_autoplay_route(show_id: int, autoplay: bool = Form(...)) -> Response:
        try:
            library.set_show_autoplay(conn, show_id, autoplay)
        except KeyError:
            raise HTTPException(404) from None
        return see_other(f"/admin/shows/{show_id}", flash=_("Saved."))

    @router.post("/admin/shows/{show_id}/sponsorblock")
    async def set_show_sponsorblock_route(request: Request, show_id: int) -> Response:  # SB-2
        form = await request.form()
        mode = form.get("mode")
        if mode not in ("default", "off", "choose"):
            raise HTTPException(422, "mode must be 'default', 'off' or 'choose'")
        if mode == "default":
            categories = None
        elif mode == "off":
            categories = []
        else:  # nothing ticked means off, as in the global setting
            categories = sponsorblock.parse_csv(",".join(str(v) for v in form.getlist("category")))
        try:
            library.set_show_sponsorblock(conn, show_id, categories)
        except KeyError:
            raise HTTPException(404) from None
        return see_other(f"/admin/shows/{show_id}", flash=_("Saved."))

    @router.post("/admin/shows/{show_id}/split-profile")
    async def set_split_profile_route(request: Request, show_id: int) -> Response:  # ES-5, ES-6, ES-9, ES-10
        form = await request.form()
        raw = {
            "threshold": str(form.get("threshold", "")).strip(), "length_hint": str(form.get("length_hint", "")).strip(),
            "snap_window": str(form.get("snap_window", "")).strip(), "ocr": bool(form.get("ocr")),
            "auto_detect": bool(form.get("auto_detect")),
            "ocr_region": [str(form.get(f"ocr_{k}", "")).strip() for k in "xywh"],
        }
        try:
            values = _parse_profile_form(raw)
            library.save_split_profile(conn, show_id, now=ctx.clock.now(), **values)
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as e:
            return render(request, "show.html", 422, error=_profile_error(e),
                          **show_or_404(request, show_id, split_form=raw))
        return see_other(f"/admin/shows/{show_id}#splitting", flash=_("Saved."))

    @router.post("/admin/shows/{show_id}/hidden")
    def set_show_hidden_route(show_id: int, hidden: bool = Form(...)) -> Response:
        try:
            library.set_show_hidden(conn, show_id, hidden)
        except KeyError:
            raise HTTPException(404) from None
        return see_other(f"/admin/shows/{show_id}", flash=_("Hidden.") if hidden else _("Unhidden."))

    @router.post("/admin/shows/{show_id}/delete")
    def delete_show_route(request: Request, show_id: int, confirm_name: str = Form("")) -> Response:
        show = library.get_show(conn, show_id)
        if show is None:
            raise HTTPException(404)
        if confirm_name.strip() != show.name:
            return render(request, "show.html", 422, error=_("Type the show's name exactly to delete it."),
                         **show_or_404(request, show_id))
        library.delete_show(conn, media_dir, show_id)
        return see_other("/admin/library", flash=_("Show deleted."))

    # --------------------------------------------------------------------- show artwork

    @router.post("/admin/shows/{show_id}/artwork/upload")
    def upload_show_artwork(request: Request, show_id: int, file: UploadFile = File(...)) -> Response:
        if library.get_show(conn, show_id) is None:
            raise HTTPException(404)
        try:
            image = clean_upload(file.file.read(MAX_UPLOAD_BYTES + 1))
        except ImageError as e:
            return render(request, "show.html", 422, error=str(e), **show_or_404(request, show_id))
        rel = save_show_artwork(media_dir, show_id, image)
        library.set_show_artwork(conn, media_dir, show_id, rel)
        return see_other(f"/admin/shows/{show_id}", flash=_("Artwork updated."))

    @router.post("/admin/shows/{show_id}/artwork/frame")
    def use_show_artwork_frame(
        request: Request, show_id: int, episode_id: int = Form(...), t: float = Form(...)
    ) -> Response:
        if library.get_show(conn, show_id) is None:
            raise HTTPException(404)
        episode = library.get_episode(conn, episode_id)
        if episode is None or episode.show_id != show_id:
            return render(request, "show.html", 422, error=_("Pick an episode of this show."),
                         **show_or_404(request, show_id))
        path = _media_path(media_dir, episode.file_path)
        if path is None:
            return render(request, "show.html", 422, error=_("That episode's video file is missing."),
                         **show_or_404(request, show_id))
        try:
            image = grab_frame(path, t)
        except ImageError as e:
            return render(request, "show.html", 422, error=str(e), **show_or_404(request, show_id))
        rel = save_show_artwork(media_dir, show_id, image)
        library.set_show_artwork(conn, media_dir, show_id, rel)
        return see_other(f"/admin/shows/{show_id}", flash=_("Artwork updated."))

    # --------------------------------------------------------------------- episodes

    @router.post("/admin/episodes/{episode_id}/rename")
    def rename_episode_route(request: Request, episode_id: int, title: str = Form(...)) -> Response:
        episode = episode_or_404(episode_id)
        try:
            library.rename_episode(conn, episode_id, title)
        except ValueError:
            return render(request, "show.html", 422, error=_("Episode title must not be empty."),
                         **show_or_404(request, episode.show_id))
        return see_other(f"/admin/shows/{episode.show_id}", flash=_("Renamed."))

    @router.post("/admin/episodes/{episode_id}/hidden")
    def set_episode_hidden_route(episode_id: int, hidden: bool = Form(...)) -> Response:
        episode = episode_or_404(episode_id)
        library.set_episode_hidden(conn, episode_id, hidden)
        return see_other(f"/admin/shows/{episode.show_id}", flash=_("Hidden.") if hidden else _("Unhidden."))

    @router.post("/admin/episodes/{episode_id}/move")
    def move_episode_route(request: Request, episode_id: int, to_show_id: int = Form(...)) -> Response:
        episode = episode_or_404(episode_id)
        try:
            library.move_episode(conn, episode_id, to_show_id)
        except KeyError:
            return render(request, "show.html", 422, error=_("Unknown target show."),
                         **show_or_404(request, episode.show_id))
        return see_other(f"/admin/shows/{to_show_id}", flash=_("Episode moved."))

    @router.post("/admin/episodes/{episode_id}/move-step")
    def move_episode_step_route(episode_id: int, direction: str = Form(...)) -> Response:
        episode = episode_or_404(episode_id)
        if direction not in ("up", "down"):
            raise HTTPException(422, "direction must be 'up' or 'down'")
        library.move_episode_step(conn, episode_id, direction)
        return see_other(f"/admin/shows/{episode.show_id}")

    @router.post("/admin/episodes/{episode_id}/delete")
    def delete_episode_route(request: Request, episode_id: int, confirm: str = Form("")) -> Response:
        episode = episode_or_404(episode_id)
        if confirm.strip().lower() != "delete":
            return render(request, "show.html", 422, error=_('Type "delete" to confirm.'),
                         **show_or_404(request, episode.show_id))
        library.delete_episode(conn, media_dir, episode_id)
        return see_other(f"/admin/shows/{episode.show_id}", flash=_("Episode deleted."))

    # --------------------------------------------------------------------- episode page and SponsorBlock (SB-4)

    def split_links(episode: library.Episode) -> dict:
        """What the episode page offers for splitting (ES-2): split it, or the part it is."""
        source = conn.execute("SELECT title, status, file_path FROM source_video WHERE id = ?",
                              (episode.source_video_id,)).fetchone()
        has_file = source is not None and source["file_path"] is not None
        part = None
        if episode.start_s is not None and source is not None:
            siblings = library.list_source_episodes(conn, episode.source_video_id)
            ids = [e.id for e in siblings]
            part = {"n": ids.index(episode.id) + 1, "of": len(ids), "title": source["title"]}
        can_split = source is not None and source["status"] == "ready" and has_file
        return {"source_id": episode.source_video_id, "part": part, "has_file": has_file,
                "can_split": can_split and part is None}

    def episode_context(episode: library.Episode, **extra) -> dict:
        show = library.get_show(conn, episode.show_id)
        info = library.get_sponsorblock_info(conn, episode.source_video_id) if episode.source_video_id else None
        pending = info is not None and jobs.has_pending_for(
            conn, info.source_id, (JobType.REDOWNLOAD, JobType.SB_RECHECK))
        window_open = info is not None and info.recheck_until is not None and info.recheck_until > ctx.clock.now()
        can_change = info is not None and info.ready and not info.split and not pending
        split = split_links(episode) if info is not None else None
        return {"split": split, 
            "nav": "library", "episode": episode, "show": show, "sb": info, "sb_pending": pending,
            "sb_window_open": window_open, "tz": ctx.config.tz,
            "sb_offer_without": can_change and info.status == "cut",
            "sb_offer_with": can_change and info.status in (None, "admin_off"), **extra,
        }

    @router.get("/admin/episodes/{episode_id}")
    def episode_page(request: Request, episode_id: int) -> Response:
        return render(request, "episode.html", **episode_context(episode_or_404(episode_id)))

    @router.post("/admin/episodes/{episode_id}/redownload")
    def redownload_route(episode_id: int, with_sb: str = Form(..., alias="sponsorblock")) -> Response:
        episode = episode_or_404(episode_id)
        if with_sb not in ("0", "1"):
            raise HTTPException(422, "sponsorblock must be '0' or '1'")
        target = f"/admin/episodes/{episode_id}"
        if episode.source_video_id is None:
            return see_other(target, flash=_("This episode has no downloaded video to download again."))
        try:
            ingest.request_redownload(conn, episode.source_video_id, with_sponsorblock=with_sb == "1",
                                      now=ctx.clock.now())
        except ingest.SourceSplit:  # SB-6 (also a pending split job)
            return see_other(target, flash=_("This video is split into episodes, so it can't be downloaded again."))
        except ingest.RedownloadPending:
            return see_other(target, flash=_("This video is already being checked or downloaded again."))
        except KeyError:
            return see_other(target, flash=_("This video can't be downloaded again right now."))
        return see_other(target, flash=_("Queued: the video will be downloaded again."))

    # --------------------------------------------------------------------- episode thumbnails / frame picking

    @router.get("/admin/episodes/{episode_id}/frame.jpg")
    def episode_frame(episode_id: int, t: float = 0.0) -> Response:
        episode = library.get_episode(conn, episode_id)
        if episode is None:
            raise HTTPException(404)
        path = _media_path(media_dir, episode.file_path)
        if path is None:
            raise HTTPException(404)
        try:
            image = grab_frame(path, t)
        except ImageError as e:
            raise HTTPException(422, str(e)) from e
        return Response(content=image, media_type="image/jpeg", headers=FRAME_CACHE)

    @router.post("/admin/episodes/{episode_id}/thumbnail/upload")
    def upload_episode_thumbnail(request: Request, episode_id: int, file: UploadFile = File(...)) -> Response:
        episode = episode_or_404(episode_id)
        try:
            image = clean_upload(file.file.read(MAX_UPLOAD_BYTES + 1))
        except ImageError as e:
            return render(request, "show.html", 422, error=str(e), **show_or_404(request, episode.show_id))
        rel = save_episode_thumbnail(media_dir, episode_id, image)
        library.set_episode_thumbnail(conn, media_dir, episode_id, rel)
        return see_other(f"/admin/shows/{episode.show_id}", flash=_("Thumbnail updated."))

    @router.post("/admin/episodes/{episode_id}/thumbnail/frame")
    def use_episode_thumbnail_frame(request: Request, episode_id: int, t: float = Form(...)) -> Response:
        episode = episode_or_404(episode_id)
        path = _media_path(media_dir, episode.file_path)
        if path is None:
            return render(request, "show.html", 422, error=_("This episode's video file is missing."),
                         **show_or_404(request, episode.show_id))
        try:
            image = grab_frame(path, t)
        except ImageError as e:
            return render(request, "show.html", 422, error=str(e), **show_or_404(request, episode.show_id))
        rel = save_episode_thumbnail(media_dir, episode_id, image)
        library.set_episode_thumbnail(conn, media_dir, episode_id, rel)
        return see_other(f"/admin/shows/{episode.show_id}", flash=_("Thumbnail updated."))

    # --------------------------------------------------------------------- images (also used by dashboard/history)

    @router.get("/admin/img/show/{show_id}.jpg")
    def show_image(show_id: int) -> Response:
        candidates = _show_image_candidates(conn, show_id)
        if candidates is None:
            raise HTTPException(404)
        for rel in candidates:
            path = _media_path(media_dir, rel)
            if path is not None:
                return FileResponse(path, headers=IMAGE_CACHE)
        raise HTTPException(404)

    @router.get("/admin/img/episode/{episode_id}.jpg")
    def episode_image(episode_id: int) -> Response:
        candidates = _episode_image_candidates(conn, episode_id)
        if candidates is None:
            raise HTTPException(404)
        for rel in candidates:
            path = _media_path(media_dir, rel)
            if path is not None:
                return FileResponse(path, headers=IMAGE_CACHE)
        raise HTTPException(404)

    return router
