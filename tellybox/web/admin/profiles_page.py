"""Profiles `/admin/profiles` (PR-1): the kids who watch. Add, rename, avatar or photo, order, delete.

Each profile has its own allowance, continue-watching list and viewing session; the picker on
the kid app shows them in this order. Allowance, counting mode and session length are on the
Settings page.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from tellybox import library, show_access
from tellybox.avatars import AVATARS
from tellybox.i18n import N_, _
from tellybox.images import MAX_UPLOAD_BYTES, ImageError, clean_upload, save_profile_photo
from tellybox.library import _transaction
from tellybox.web.admin.common import AdminContext, render, see_other
from tellybox.web.cast_client import CastUnavailable

NAME_MAX = 40

# Screen-reader names of the built-in avatars (the picker shows the pictures).
AVATAR_LABELS = {
    "fox": N_("Fox"), "bear": N_("Bear"), "rabbit": N_("Rabbit"), "owl": N_("Owl"),
    "cat": N_("Cat"), "dog": N_("Dog"), "frog": N_("Frog"), "penguin": N_("Penguin"),
}


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn, media_dir = ctx.conn, ctx.config.media_dir

    def ordered() -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT id, name, picture_path, avatar FROM profile ORDER BY sort_order, id"
        ).fetchall()

    def get_or_404(profile_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT id, name, picture_path, avatar FROM profile WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        return row

    async def cast_state() -> dict | None:
        try:
            return await ctx.cast.state()
        except CastUnavailable:
            return None

    async def page(request: Request, status_code: int = 200, **extra) -> HTMLResponse:
        state = await cast_state()
        timers = {p["profile_id"]: p for p in ((state or {}).get("timer") or {}).get("profiles", [])}
        playing = set(((state or {}).get("now_playing") or {}).get("profile_ids") or [])
        counts = show_access.visible_count_by_profile(conn)
        profiles = [{**dict(r), "used_s": timers[r["id"]]["used_s"] if r["id"] in timers else None,
                     "watching": r["id"] in playing, "visible_shows": counts.get(r["id"], 0)} for r in ordered()]
        return render(request, "profiles.html", status_code, nav="profiles", profiles=profiles,
                      avatars=[(key, _(AVATAR_LABELS[key])) for key in AVATARS], limit=NAME_MAX, **extra)

    def check(name: str, avatar: str) -> tuple[str, str | None, str | None]:
        """(name, avatar, error) from the form fields; `avatar` "" means none."""
        name = name.strip()
        if not name:
            return name, None, _("Enter a name.")
        if len(name) > NAME_MAX:
            return name, None, _("The name can be at most %(max)d characters.") % {"max": NAME_MAX}
        if avatar and avatar not in AVATARS:
            return name, None, _("Choose one of the pictures.")
        return name, avatar or None, None

    @router.get("/admin/profiles")
    async def profiles_page(request: Request) -> HTMLResponse:
        return await page(request)

    @router.post("/admin/profiles")
    async def add_profile(request: Request, name: str = Form(""), avatar: str = Form(""),
                          copy_from: str = Form("")) -> Response:
        name, avatar_key, error = check(name, avatar)
        source = None
        if not error and copy_from:  # PR-5, AD-8: a one-off copy of another profile's shows ("" = start with none)
            source = int(copy_from) if copy_from.isdigit() else None
            if source is None or conn.execute("SELECT 1 FROM profile WHERE id = ?", (source,)).fetchone() is None:
                error = _("Choose one of the existing profiles.")
        if error:
            return await page(request, 422, error=error)
        with _transaction(conn):
            last = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM profile").fetchone()[0]
            cur = conn.execute(
                "INSERT INTO profile (name, avatar, sort_order, created_at) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (name, avatar_key, last + 1),
            )
            if source is not None:
                show_access.copy_from(conn, source, cur.lastrowid)
        return see_other("/admin/profiles", flash=_("Profile added."))

    @router.post("/admin/profiles/{profile_id}")
    async def edit_profile(request: Request, profile_id: int, name: str = Form(""), avatar: str = Form("")) -> Response:
        get_or_404(profile_id)
        name, avatar_key, error = check(name, avatar)
        if error:
            return await page(request, 422, error=error)
        conn.execute("UPDATE profile SET name = ?, avatar = ? WHERE id = ?", (name, avatar_key, profile_id))
        return see_other("/admin/profiles", flash=_("Saved."))

    @router.post("/admin/profiles/{profile_id}/move")
    async def move_profile(request: Request, profile_id: int, direction: str = Form("")) -> Response:
        get_or_404(profile_id)
        if direction not in ("up", "down"):
            return await page(request, 422, error=_("Choose up or down."))
        ids = [r["id"] for r in ordered()]
        i = ids.index(profile_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        with _transaction(conn):  # rewrite every position, so equal sort_orders can't stay tied
            conn.executemany("UPDATE profile SET sort_order = ? WHERE id = ?", [(n + 1, pid) for n, pid in enumerate(ids)])
        return see_other("/admin/profiles")

    @router.post("/admin/profiles/{profile_id}/photo")
    def upload_photo(request: Request, profile_id: int, file: UploadFile = File(...)) -> Response:
        get_or_404(profile_id)
        try:
            image = clean_upload(file.file.read(MAX_UPLOAD_BYTES + 1))
        except ImageError as e:
            return render(request, "profiles.html", 422, **_sync_context(error=str(e)))
        library.set_profile_picture(conn, media_dir, profile_id, save_profile_photo(media_dir, profile_id, image))
        return see_other("/admin/profiles", flash=_("Photo updated."))

    @router.post("/admin/profiles/{profile_id}/photo/remove")
    async def remove_photo(request: Request, profile_id: int) -> Response:
        get_or_404(profile_id)
        library.set_profile_picture(conn, media_dir, profile_id, None)
        return see_other("/admin/profiles", flash=_("Photo removed."))

    @router.post("/admin/profiles/{profile_id}/delete")
    async def delete_profile(request: Request, profile_id: int) -> Response:
        get_or_404(profile_id)
        if len(ordered()) <= 1:
            return await page(request, 409, error=_("This is the last profile; it can't be deleted."))
        state = await cast_state()
        if state is None:
            return await page(request, 409, error=_("Cast service unreachable; can't tell whether this profile is watching."))
        if profile_id in ((state.get("now_playing") or {}).get("profile_ids") or []):
            return await page(request, 409, error=_("This profile is watching right now. Stop the TV first."))
        library.delete_profile(conn, media_dir, profile_id)
        return see_other("/admin/profiles", flash=_("Profile deleted."))

    def _sync_context(**extra) -> dict:
        """The page context without the cast state (the upload handler is a plain, threaded function)."""
        counts = show_access.visible_count_by_profile(conn)
        profiles = [{**dict(r), "used_s": None, "watching": False, "visible_shows": counts.get(r["id"], 0)}
                    for r in ordered()]
        return {"nav": "profiles", "profiles": profiles,
                "avatars": [(key, _(AVATAR_LABELS[key])) for key in AVATARS], "limit": NAME_MAX, **extra}

    return router
