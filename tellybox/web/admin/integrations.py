"""Integrations `/admin/integrations` (HA-1): API tokens for Home Assistant and similar tools.

A token's secret is shown once, in the response to the create POST itself: never in a URL, a
redirect or a flash cookie. Only its hash is stored (`tellybox.api_tokens`). Revoked tokens stay
listed, greyed out.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from tellybox import api_tokens
from tellybox.i18n import _
from tellybox.web.admin.common import AdminContext, render, see_other


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn, tz = ctx.conn, ctx.config.tz

    def stamp(moment) -> str | None:
        return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") if moment else None

    def page(request: Request, status_code: int = 200, **extra) -> HTMLResponse:
        tokens = [
            {"id": t.id, "name": t.name, "control": t.allows(api_tokens.CONTROL), "revoked": t.revoked,
             "created": stamp(t.created_at), "last_used": stamp(t.last_used_at), "revoked_at": stamp(t.revoked_at)}
            for t in api_tokens.list_tokens(conn)
        ]
        return render(request, "integrations.html", status_code, nav="integrations", tokens=tokens,
                      name_max=api_tokens.NAME_MAX, **extra)

    @router.get("/admin/integrations")
    def integrations_page(request: Request) -> HTMLResponse:
        return page(request)

    @router.post("/admin/integrations")
    def create_token(request: Request, name: str = Form(""), scope: str = Form(api_tokens.CONTROL)) -> Response:
        name = name.strip()
        if not name:
            return page(request, 422, error=_("Enter a name."))
        if len(name) > api_tokens.NAME_MAX:
            return page(request, 422, error=_("The name can be at most %(max)d characters.") % {"max": api_tokens.NAME_MAX})
        if scope not in api_tokens.SCOPES:
            return page(request, 422, error=_("Choose what the token may do."))
        _id, secret = api_tokens.create_token(conn, name, [scope], ctx.clock.now())
        # 200 rather than a redirect: the secret exists only in this response.
        return page(request, 200, new_secret=secret, new_name=name)

    @router.post("/admin/integrations/{token_id}/revoke")
    def revoke_token(token_id: int) -> Response:
        if api_tokens.get_token(conn, token_id) is None:
            raise HTTPException(404)
        api_tokens.revoke_token(conn, token_id, ctx.clock.now())
        return see_other("/admin/integrations", flash=_("Token revoked."))

    return router
