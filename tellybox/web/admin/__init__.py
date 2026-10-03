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
from tellybox.oidc import BROWSER_COOKIE, LOGIN_TTL, OidcError, is_admin
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
    conn, clock, oidc = ctx.conn, ctx.clock, ctx.oidc
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

    def login_form(request: Request, next: str, status_code: int = 200, error: str | None = None) -> Response:
        """The sign-in page; with OIDC configured, its button comes first (AD-6)."""
        return render(request, "login.html", status_code=status_code, next=next, error=error,
                      oidc_enabled=oidc is not None)

    @public.get("/admin/login")
    def login_page(request: Request, next: str | None = None) -> Response:
        if auth.is_locked(conn):
            return see_other("/admin/setup")
        if auth.touch_session(conn, request.cookies.get(auth.SESSION_COOKIE), clock.now()):
            return see_other(_safe_next(next))
        return login_form(request, _safe_next(next))

    @public.post("/admin/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/admin")) -> Response:
        if auth.is_locked(conn):
            return see_other("/admin/setup")
        if not same_origin(request):
            return login_form(request, _safe_next(next), 403, _("This sign-in form must be sent from this page."))
        now = clock.now()
        address = client_address(request)
        wait = auth.throttle_wait_s(conn, address, now)
        if wait > 0:
            return login_form(request, _safe_next(next), 429,
                              _("Too many attempts. Try again in %(seconds)d s.") % {"seconds": int(wait + 0.999)})
        if not auth.check_password(conn, password):
            auth.record_failure(conn, address, now)
            return login_form(request, _safe_next(next), 401, _("Wrong password."))
        auth.clear_failures(conn, address)
        token = auth.create_session(conn, now)
        response = see_other(_safe_next(next))
        set_session_cookie(response, request, token)
        return response

    if oidc is not None:
        # AD-6: only mounted with OIDC configured (otherwise a 404). No password throttle: there's
        # no password to guess, and each state works once. While locked, setup comes first (DP-4).

        def set_browser_cookie(response: Response, request: Request, value: str) -> None:
            """Binds the sign-in to this browser. SameSite=Lax, unlike the Strict session cookie:
            the provider sends the browser back with a cross-site top-level GET, which carries Lax
            cookies but not Strict ones. It only lives on /admin/oidc, for one sign-in."""
            response.set_cookie(BROWSER_COOKIE, value, path="/admin/oidc", httponly=True, samesite="lax",
                                secure=is_https(request), max_age=int(LOGIN_TTL.total_seconds()))

        def clear_browser_cookie(response: Response, request: Request) -> Response:
            response.delete_cookie(BROWSER_COOKIE, path="/admin/oidc", httponly=True, samesite="lax",
                                   secure=is_https(request))
            return response

        @public.get("/admin/oidc/start")
        async def oidc_start(request: Request, next: str | None = None) -> Response:
            if auth.is_locked(conn):
                return see_other("/admin/setup")
            try:
                url, browser = await oidc.begin(conn, _safe_next(next), clock.now())
            except OidcError as exc:
                log.warning("admin sign-in with OIDC could not start: %s", exc)
                return login_form(request, _safe_next(next), 502, _("Sign-in with OIDC failed. Try again."))
            response = see_other(url)
            set_browser_cookie(response, request, browser)
            return response

        @public.get("/admin/oidc/callback")
        async def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
            """The provider sends the browser back here, a navigation a cross-site page started.

            Browsers treat every hop of such a redirect chain as cross-site, so a 303 to `next` would
            arrive without our SameSite=Strict cookie and bounce to the sign-in page. Success is
            therefore a 200 page that sets the cookie and moves on itself (meta refresh, plus a
            link): a navigation this page starts is same-site, and carries the cookie."""
            if auth.is_locked(conn):
                return see_other("/admin/setup")
            now = clock.now()
            try:
                if error:
                    raise OidcError(f"the provider answered {error!r} "
                                    f"({request.query_params.get('error_description', '')!r})")
                if not code or not state:
                    raise OidcError("callback without code or state")
                identity, next = await oidc.finish(conn, code, state, request.cookies.get(BROWSER_COOKIE), now)
            except OidcError as exc:
                log.warning("admin sign-in with OIDC failed: %s", exc)
                return clear_browser_cookie(
                    login_form(request, "/admin", 400, _("Sign-in with OIDC failed. Try again.")), request)
            if not is_admin(identity, oidc.config.admin_group):
                log.warning("admin sign-in with OIDC refused: %s (%s) is not in group %r",
                            identity.name, identity.subject, oidc.config.admin_group)
                return clear_browser_cookie(
                    login_form(request, next, 403, _("Your account may not use the Tellybox admin.")), request)
            log.info("admin signed in with OIDC as %s (%s)", identity.name, identity.subject)
            token = auth.create_session(conn, now)
            response = render(request, "signed_in.html", next=next)
            # The URL carries the code and state: keep it out of caches and any Referer.
            response.headers["Referrer-Policy"] = "no-referrer"
            set_session_cookie(response, request, token)
            return clear_browser_cookie(response, request)

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
