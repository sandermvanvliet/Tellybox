"""History `/admin/history` (AD-4): the last 21 watch days, newest first."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from tellybox import store
from tellybox.history import history_days
from tellybox.i18n import N_, _
from tellybox.web.admin.common import AdminContext, render

OVERRIDE_LABELS = {
    "extra_minutes": N_("Added time"),
    "unlimited": N_("Unlimited today"),
    "block": N_("Block"),
    "stop_now": N_("Stop now"),
    "clear": N_("Cleared unlimited and block"),  # HA-5
}


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()

    @router.get("/admin/history")
    def history_page(request: Request, profile: str | None = None) -> HTMLResponse:
        reset_time = store.timer_settings(ctx.conn, ctx.config.tz).reset_time
        profiles = ctx.conn.execute(
            "SELECT id, name, avatar, picture_path FROM profile ORDER BY sort_order, id").fetchall()
        selected = int(profile) if profile and profile.isdigit() else None  # anything else shows everyone
        days = history_days(ctx.conn, ctx.clock.now(), ctx.config.tz, reset_time, profile_id=selected)
        has_any = any(d.episodes or d.overrides for d in days)
        return render(
            request, "history.html", nav="history",
            days=days, tz=ctx.config.tz, profiles=profiles, selected_profile=selected, has_any=has_any, override_labels={kind: _(label) for kind, label in OVERRIDE_LABELS.items()},
        )

    return router
