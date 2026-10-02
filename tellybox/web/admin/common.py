"""Shared plumbing for the admin pages: the request guard, templates and form helpers.

Every admin route is mounted behind `AdminGuard` (AD-1, NF-2): it refuses requests
without a live session and form posts from anywhere but the admin pages (Origin check).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from tellybox.clock import Clock
from tellybox.config import Config
from tellybox import auth, i18n, sponsorblock
from tellybox.web.admin.js_strings import js_catalog

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
FLASH_COOKIE = "tb_flash"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

templates = Jinja2Templates(directory=TEMPLATES_DIR)
# NF-13: {{ _("...") }}, {{ ngettext(...) }} and {% trans %} in the request's language. New-style
# gettext: placeholders are keyword arguments, escaped and filled in by Jinja.
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(i18n.gettext, i18n.ngettext, newstyle=True)
templates.env.globals["current_locale"] = i18n.current_locale
templates.env.globals["js_catalog"] = js_catalog
templates.env.globals["format_date"] = i18n.format_date
templates.env.globals["sb_categories"] = list(sponsorblock.CATEGORIES)


def _sb_label(key: str) -> str:
    """A SponsorBlock category's label in the current language (unknown keys as they are)."""
    return i18n.gettext(sponsorblock.CATEGORIES[key]) if key in sponsorblock.CATEGORIES else key


templates.env.globals["sb_label"] = _sb_label
templates.env.filters["sb_label"] = _sb_label  # for map("sb_label")


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    m = int(round(seconds / 60))
    if m >= 60:
        return i18n.gettext("%(hours)s h %(minutes)s min") % {"hours": m // 60, "minutes": f"{m % 60:02d}"}
    return i18n.gettext("%(minutes)s min") % {"minutes": m}


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# Size units; translatable because some languages write them differently (French "Mo").
BYTE_UNITS = {"KB": i18n.N_("KB"), "MB": i18n.N_("MB"), "GB": i18n.N_("GB")}


def _bytes(n: int | None) -> str:
    if n is None:
        return "–"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return i18n.gettext("%(size)s B") % {"size": round(size)}
            return f"{size:.1f} {i18n.gettext(BYTE_UNITS[unit])}"
        size /= 1024
    return f"{size:.1f} TB"


templates.env.filters["minutes"] = _minutes  # 1500 -> "25 min"
templates.env.filters["duration"] = _duration  # 1500 -> "25:00"
templates.env.filters["bytes"] = _bytes  # 1536 -> "1.5 KB"


@dataclass
class AdminContext:
    """Handed to every admin router factory."""

    config: Config
    conn: sqlite3.Connection
    clock: Clock
    cast: object  # CastClient-like (tellybox.web.cast_client)
    ytdlp: object  # YtDlp-like, for previews (tellybox.ytdlp)


# --------------------------------------------------------------------------- request checks


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    """HTTPS directly, or via a local proxy such as `tailscale serve`."""
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def same_origin(request: Request) -> bool:
    """A form post must come from the admin pages themselves: its Origin (or Referer) host must be ours."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return False
    ours = {h.lower() for h in (request.headers.get("host"), request.headers.get("x-forwarded-host")) if h}
    return urlsplit(source).netloc.lower() in ours


class AdminLocked(Exception):
    """No admin password is configured; every admin page sends the browser to the setup page (DP-4)."""


class AdminGuard:
    """Router dependency: live session required; unsafe methods must pass the Origin check."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock

    def __call__(self, request: Request) -> None:
        if auth.is_locked(self.conn):
            raise AdminLocked()
        if request.method not in SAFE_METHODS and not same_origin(request):
            raise HTTPException(403, "cross-origin request refused")
        if not auth.touch_session(self.conn, request.cookies.get(auth.SESSION_COOKIE), self.clock.now()):
            if request.method == "GET" and "text/html" in request.headers.get("accept", "text/html"):
                target = quote(request.url.path, safe="/")
                raise HTTPException(303, headers={"Location": f"/admin/login?next={target}"})
            raise HTTPException(401, "sign in first")
        request.state.admin_session_ok = True  # the app middleware refreshes the cookie's lifetime


# --------------------------------------------------------------------------- responses


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    """Render an admin page. Templates extend base.html; `nav` names the active menu item."""
    flash = unquote(request.cookies.get(FLASH_COOKIE) or "") or None
    response = templates.TemplateResponse(request, template, {"flash": flash, **context}, status_code=status_code)
    if flash:
        response.delete_cookie(FLASH_COOKIE, path="/admin")
    response.headers["Cache-Control"] = "no-store"
    return response


def see_other(url: str, flash: str | None = None) -> RedirectResponse:
    """Post/redirect/get; `flash` is shown once on the next page."""
    response = RedirectResponse(url, status_code=303)
    if flash:
        response.set_cookie(FLASH_COOKIE, quote(flash), path="/admin", httponly=True, samesite="strict", max_age=60)
    return response
