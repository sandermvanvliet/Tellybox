"""Show access `/admin/access` (PR-5, AD-8): a show x profile matrix of ticks, and the "Visible to" checklist
on the show settings page. The allow-list lives in `tellybox.show_access`; assigning is a deliberate admin
action, there is no endpoint for it in the admin API (HA-10).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response

from tellybox import library, show_access
from tellybox.i18n import _
from tellybox.library import _transaction
from tellybox.web.admin.common import AdminContext, access_profiles, render, see_other


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn = ctx.conn

    @router.get("/admin/access")
    def access_page(request: Request) -> Response:
        profiles = access_profiles(conn)
        shows = library.list_shows(conn)
        granted = {(r["profile_id"], r["show_id"]) for r in conn.execute("SELECT profile_id, show_id FROM profile_show")}
        return render(request, "access.html", nav="access", profiles=profiles, shows=shows, granted=granted)

    @router.post("/admin/access")
    def save_matrix(cell: list[str] = Form([])) -> Response:
        """`cell` holds "profile_id:show_id" for every ticked box; what is not ticked is removed."""
        profile_ids = {p["id"] for p in access_profiles(conn)}
        show_ids = {s.id for s in library.list_shows(conn)}
        wanted: dict[int, set[int]] = {pid: set() for pid in profile_ids}
        for value in cell:
            try:
                pid, sid = (int(x) for x in value.split(":"))
            except ValueError:
                raise HTTPException(422, "bad cell") from None
            if pid in profile_ids and sid in show_ids:  # a stale page may name a profile or show that is gone
                wanted[pid].add(sid)
        with _transaction(conn):
            for pid, shows in wanted.items():
                show_access.set_profile_shows(conn, pid, shows)
        return see_other("/admin/access", flash=_("Saved."))

    @router.post("/admin/shows/{show_id}/access")
    def save_show_access(show_id: int, profile: list[int] = Form([])) -> Response:
        if library.get_show(conn, show_id) is None:
            raise HTTPException(404)
        known = {p["id"] for p in access_profiles(conn)}
        with _transaction(conn):
            show_access.set_show_profiles(conn, show_id, [p for p in profile if p in known])
        return see_other(f"/admin/shows/{show_id}#access", flash=_("Saved."))

    return router
