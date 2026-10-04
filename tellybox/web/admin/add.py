"""Add video or playlist (CI-1, CI-7): preview a URL, then approve it (or hold it) for download.

Previews are kept server-side (a preview id -> VideoInfo or PlaylistInfo) so the form only
ever carries the id, plus for a playlist the selected video ids, which are checked against
the stored preview; nothing else about the videos round-trips through the browser.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request

from tellybox import ingest, library
from tellybox import ytdlp as ytdlp_module
from tellybox.i18n import N_, _, ngettext
from tellybox.web.admin.common import AdminContext, render, see_other
from tellybox.ytdlp import PlaylistInfo, VideoInfo, YtDlpError

PREVIEW_TTL = timedelta(minutes=30)
PREVIEW_CAP = 50  # small size cap on the in-memory preview store

EXPIRED = N_("That preview has expired; fetch it again.")

# PlaylistEntry.unavailable_reason -> label shown next to the greyed-out row
UNAVAILABLE_LABELS = {
    "private": N_("Private"),
    "deleted": N_("Deleted"),
    "live": N_("Live stream"),
    "upcoming": N_("Not out yet"),
    "unavailable": N_("Unavailable"),
}


def playlist_flash(added: int, skipped: int, *, held: bool) -> str:
    """"Added 23 videos (held), skipped 2"; the skipped part only when something was skipped."""
    values = {"num": added, "skipped": skipped}
    if held and added:
        if skipped:
            text = ngettext("Added %(num)d video (held), skipped %(skipped)d",
                            "Added %(num)d videos (held), skipped %(skipped)d", added)
        else:
            text = ngettext("Added %(num)d video (held)", "Added %(num)d videos (held)", added)
    elif skipped:
        text = ngettext("Added %(num)d video, skipped %(skipped)d", "Added %(num)d videos, skipped %(skipped)d",
                        added)
    else:
        text = ngettext("Added %(num)d video", "Added %(num)d videos", added)
    return text % values


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    # Scoped to this router instance (one per app), not module-global: each app gets its
    # own store, so tests (and the web process) never leak previews across instances.
    previews: dict[str, tuple[VideoInfo | PlaylistInfo, datetime]] = {}

    def sweep(now: datetime) -> None:
        for pid, (_item, created) in list(previews.items()):
            if now - created > PREVIEW_TTL:
                del previews[pid]
        while len(previews) > PREVIEW_CAP:
            oldest = min(previews, key=lambda k: previews[k][1])
            del previews[oldest]

    def store(item: VideoInfo | PlaylistInfo, now: datetime) -> str:
        preview_id = secrets.token_urlsafe(9)
        previews[preview_id] = (item, now)
        sweep(now)
        return preview_id

    def lookup(preview_id: str, kind: type, now: datetime):
        sweep(now)
        entry = previews.get(preview_id)
        if entry is None or not isinstance(entry[0], kind):
            return None
        return entry[0]

    def show_for(info: VideoInfo) -> tuple[str | None, str | None]:
        """(existing show name, "new show" channel name) — exactly one is set."""
        show = library.find_show_by_channel(ctx.conn, info.channel_id) if info.channel_id else None
        if show:
            return show.name, None
        return None, info.channel_name or _("Unknown channel")

    def playlist_rows(playlist: PlaylistInfo) -> list[dict]:
        """One row per entry: the entry plus why it can't be ticked (None = selectable)."""
        existing = ingest.existing_youtube_ids(ctx.conn, [e.youtube_id for e in playlist.entries])
        seen: set[str] = set()
        rows = []
        for entry in playlist.entries:
            if entry.youtube_id in existing:
                reason = _("Already added")
            elif entry.unavailable_reason:
                reason = _(UNAVAILABLE_LABELS.get(entry.unavailable_reason, "Unavailable"))
            elif entry.youtube_id in seen:
                reason = _("Listed twice")
            else:
                reason = None
            seen.add(entry.youtube_id)
            rows.append({"entry": entry, "reason": reason})
        return rows

    def render_playlist(request: Request, playlist: PlaylistInfo, preview_id: str, status_code: int = 200,
                        **extra):
        rows = playlist_rows(playlist)
        return render(request, "add.html", status_code=status_code, nav="add", url=playlist.url,
                      playlist=playlist, preview_id=preview_id, rows=rows,
                      selectable=sum(1 for r in rows if r["reason"] is None), **extra)

    @router.get("/admin/add")
    def add_page(request: Request):
        return render(request, "add.html", nav="add")

    @router.post("/admin/add/preview")
    def preview(request: Request, url: str = Form(""), mode: str = Form("")):
        url = url.strip()
        if not url or urlsplit(url).scheme not in ("http", "https"):
            return render(request, "add.html", status_code=422, nav="add", url=url,
                         error=_("Enter a video or playlist URL (http:// or https://)."))
        kind = ytdlp_module.classify_url(url)
        if kind == "channel":
            return render(request, "add.html", nav="add", url=url, channel_url=url)  # CS-1: subscribe instead
        if kind == "both":
            if mode not in ("video", "playlist"):
                return render(request, "add.html", nav="add", url=url, choose=True)
            kind = mode
        # `mode` only picks between the two readings of a "both" URL; otherwise the URL decides.
        now = ctx.clock.now()
        sweep(now)
        try:
            if kind == "playlist":
                playlist = ingest.preview_playlist(ctx.ytdlp, url)
            else:
                info = ingest.preview(ctx.ytdlp, url)
        except YtDlpError as exc:
            error = _("%(error)s It may work later.") % {"error": exc.message} if exc.retryable else exc.message
            return render(request, "add.html", status_code=422, nav="add", url=url, error=error)
        if kind == "playlist":
            return render_playlist(request, playlist, store(playlist, now))
        preview_id = store(info, now)
        show_name, new_show_channel = show_for(info)
        return render(request, "add.html", nav="add", url=url, info=info, preview_id=preview_id,
                     show_name=show_name, new_show_channel=new_show_channel)

    @router.post("/admin/add")
    def add(request: Request, preview_id: str = Form(...), action: str = Form(...)):
        now = ctx.clock.now()
        info = lookup(preview_id, VideoInfo, now)
        if info is None:
            return render(request, "add.html", status_code=422, nav="add", error=_(EXPIRED))
        try:
            ingest.add(ctx.conn, info, publish=action != "hold", now=now)
        except ingest.AlreadyAdded as exc:
            del previews[preview_id]
            return render(request, "add.html", status_code=422, nav="add",
                         error=_("That video was already added."),
                         already_added={"source_video_id": exc.source_video_id, "status": exc.status})
        del previews[preview_id]
        return see_other("/admin/jobs", flash=_("Added") if action != "hold" else _("Added and held"))

    @router.post("/admin/add/playlist")
    def add_playlist(
        request: Request,
        preview_id: str = Form(...),
        video: list[str] = Form([]),
        hold: str = Form(""),
    ):
        now = ctx.clock.now()
        playlist = lookup(preview_id, PlaylistInfo, now)
        if playlist is None:
            return render(request, "add.html", status_code=422, nav="add", error=_(EXPIRED))
        known = {e.youtube_id for e in playlist.entries}
        if any(v not in known for v in video):
            # Not something the page can produce: refuse the whole request, write nothing.
            return render(request, "add.html", status_code=422, nav="add",
                         error=_("That selection doesn't match the preview; fetch it again."))
        chosen = set(video)
        selected = list(dict.fromkeys(e.youtube_id for e in playlist.entries if e.youtube_id in chosen))
        if not selected:
            return render_playlist(request, playlist, preview_id, status_code=422,
                                   error=_("Tick at least one video to add."))
        held = bool(hold)
        try:
            result = ingest.add_playlist(ctx.conn, playlist, selected, publish=not held, now=now)
        except ValueError:
            return render(request, "add.html", status_code=422, nav="add",
                         error=_("That selection doesn't match the preview; fetch it again."))
        del previews[preview_id]
        return see_other("/admin/jobs", flash=playlist_flash(len(result.added), len(result.skipped), held=held))

    return router
