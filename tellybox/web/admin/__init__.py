"""Admin pages (AD-1..AD-5): server-rendered Jinja2 with plain forms, mounted under /admin.

Page routers live in their own modules; each exposes `create_router(ctx) -> APIRouter`
with full `/admin/...` paths and is mounted behind the AdminGuard. Sign-in lives here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response

from tellybox import auth
from tellybox.i18n import _
from tellybox.web.admin import add, dashboard, history, integrations, jobs_page, library_pages, profiles_page, settings_page, split_pages
from tellybox.web.admin.common import (
    STATIC_DIR,
    AdminContext,
    AdminGuard,
    AdminLocked,
    client_address,
    is_https,
    render,
    same_origin,
    see_other,
)
from tellybox.web.static_files import NoCacheStaticFiles

log = logging.getLogger(__name__)

PAGE_MODULES = (dashboard, add, jobs_page, library_pages, profiles_page, settings_page, integrations, history, split_pages)


def _safe_next(target: str | None) -> str:
    """Only redirect to our own admin pages after sign-in."""
    if target and target.startswith("/admin") and not target.startswith("//") and "\\" not in target:
        return target
    return "/admin"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(auth.SESSION_COOKIE, token, path="/admin", httponly=True, samesite="strict",
                        secure=is_https(request), max_age=int(auth.SESSION_IDLE.total_seconds()))


def mount_admin(app: FastAPI, ctx: AdminContext) -> None:
    conn, clock = ctx.conn, ctx.clock
    auth.install_password(conn, ctx.config.admin_password)
    if auth.is_locked(conn):  # DP-4: no password yet; a fresh code on every start
        code = auth.new_setup_code(conn, clock.now())
        log.warning("admin setup code: %s (open /admin/setup to choose the admin password)", code)

    @app.middleware("http")
    async def slide_session_cookie(request: Request, call_next):
        """Each admin visit restarts the 30-day clock, in the browser as well as in the database."""
        response = await call_next(request)
        sets_session = any(c.startswith(f"{auth.SESSION_COOKIE}=") for c in response.headers.getlist("set-cookie"))
        if getattr(request.state, "admin_session_ok", False) and not sets_session:
            set_session_cookie(response, request, request.cookies[auth.SESSION_COOKIE])
        return response

    @app.exception_handler(AdminLocked)
    async def locked(request: Request, exc: AdminLocked) -> Response:
        """No password yet: browsers go to the setup page; scripts and form posts get a plain 401."""
        if request.method == "GET" and "text/html" in request.headers.get("accept", "text/html"):
            return see_other("/admin/setup")
        return Response("admin setup required", status_code=401)

    public = APIRouter()

    def setup_page(request: Request, status_code: int = 200, error: str | None = None) -> Response:
        return render(request, "setup.html", status_code=status_code, error=error)

    @public.get("/admin/setup")
    def setup_form(request: Request) -> Response:
        if not auth.is_locked(conn):
            return see_other("/admin/login")
        return setup_page(request)

    @public.post("/admin/setup")
    def setup(request: Request, code: str = Form(""), password: str = Form(""),
              password2: str = Form("")) -> Response:
        if not auth.is_locked(conn):
            return see_other("/admin/login")
        if not same_origin(request):
            return setup_page(request, 403, _("This form must be sent from this page."))
        now = clock.now()
        address = client_address(request)
        wait = auth.throttle_wait_s(conn, address, now)
        if wait > 0:
            return setup_page(request, 429,
                              _("Too many attempts. Try again in %(seconds)d s.") % {"seconds": int(wait + 0.999)})
        if not auth.check_setup_code(conn, code):
            auth.record_failure(conn, address, now)
            return setup_page(request, 401, _("Wrong setup code."))
        if len(password) < auth.SETUP_MIN_PASSWORD:
            return setup_page(request, 400, _("The password must be at least %(n)d characters.")
                              % {"n": auth.SETUP_MIN_PASSWORD})
        if password != password2:
            return setup_page(request, 400, _("The two passwords are not the same."))
        if not auth.set_ui_password(conn, password):
            return see_other("/admin/login")
        auth.clear_failures(conn, address)
        token = auth.create_session(conn, now)
        response = see_other("/admin")
        set_session_cookie(response, request, token)
        return response

    @public.get("/admin/login")
    def login_page(request: Request, next: str | None = None) -> Response:
        if auth.is_locked(conn):
            return see_other("/admin/setup")
        if auth.touch_session(conn, request.cookies.get(auth.SESSION_COOKIE), clock.now()):
            return see_other(_safe_next(next))
        return render(request, "login.html", next=_safe_next(next))

    @public.post("/admin/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/admin")) -> Response:
        if auth.is_locked(conn):
            return see_other("/admin/setup")
        if not same_origin(request):
            return render(request, "login.html", status_code=403, next=_safe_next(next),
                          error=_("This sign-in form must be sent from this page."))
        now = clock.now()
        address = client_address(request)
        wait = auth.throttle_wait_s(conn, address, now)
        if wait > 0:
            return render(request, "login.html", status_code=429, next=_safe_next(next),
                          error=_("Too many attempts. Try again in %(seconds)d s.") % {"seconds": int(wait + 0.999)})
        if not auth.check_password(conn, password):
            auth.record_failure(conn, address, now)
            return render(request, "login.html", status_code=401, next=_safe_next(next),
                          error=_("Wrong password."))
        auth.clear_failures(conn, address)
        token = auth.create_session(conn, now)
        response = see_other(_safe_next(next))
        set_session_cookie(response, request, token)
        return response

    guard = AdminGuard(conn, clock)

    @public.post("/admin/logout", dependencies=[Depends(guard)])
    def logout(request: Request) -> Response:
        auth.end_session(conn, request.cookies.get(auth.SESSION_COOKIE))
        response = see_other("/admin/login")
        response.delete_cookie(auth.SESSION_COOKIE, path="/admin")
        return response

    app.include_router(public)
    for module in PAGE_MODULES:
        app.include_router(module.create_router(ctx), dependencies=[Depends(guard)])
    # Stylesheet and scripts only; no data behind this mount. Revalidated on every load (see NoCacheStaticFiles).
    app.mount("/admin/static", NoCacheStaticFiles(directory=STATIC_DIR), name="admin-static")
