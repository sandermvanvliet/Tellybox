"""FastAPI application for the `web` service.

Serves the signed media route the Chromecast fetches episodes from (NF-3), the kid
app (static shell + /api/kid, docs/kid-api.md), the admin pages (/admin) and a health check.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tellybox import db, library
from tellybox.clock import Clock, SystemClock
from tellybox.config import Config
from tellybox.media_urls import verify
from tellybox.oidc import OidcClient
from tellybox.ytdlp import YtDlp
from tellybox.web import api, kid
from tellybox.web.admin import mount_admin
from tellybox.web.admin.common import AdminContext
from tellybox.web.cast_client import CastClient
from tellybox.web.hub import KidHub
from tellybox.web.locale import LocaleMiddleware
from tellybox.web.static_files import NoCacheStaticFiles

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
RECEIVER_DIR = Path(__file__).parent / "receiver"  # the Tellybox Cast receiver page (CR-1)
ADMIN_REFRESH_S = 10.0  # the admin hub re-reduces this often (jobs, disk, profile settings)


def resolve_media_file(media_dir: Path, file_path: str) -> Path | None:
    """Absolute path of an episode file, or None if missing or outside the media dir."""
    root = media_dir.resolve()
    candidate = (root / file_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def create_app(
    config: Config,
    conn: sqlite3.Connection | None = None,
    clock: Clock | None = None,
    cast=None,
    *,
    static_dir: Path | None = None,
    ytdlp=None,
    oidc=None,
) -> FastAPI:
    """`cast` is a CastClient-like object (tests pass a fake); by default one for the configured cast service.
    `ytdlp` is a YtDlp-like object for admin previews; by default the updatable install in the data dir.
    `oidc` is an OidcClient-like object for admin sign-in (AD-6); by default one for `config.oidc`, if set.
    """
    if conn is None:
        db.open_db(config.db_path).close()  # migrate once
        conn = db.PerThreadConnection(config.db_path)  # handlers run concurrently in a thread pool
    clock = clock or SystemClock()
    owns_cast = cast is None
    cast = cast if cast is not None else CastClient.from_config(config)
    static_dir = static_dir or STATIC_DIR
    ytdlp = ytdlp if ytdlp is not None else YtDlp(config.data_dir / "tools" / "yt-dlp")
    owns_oidc = oidc is None and config.oidc is not None
    if owns_oidc:
        oidc = OidcClient(config.oidc)
    hub = KidHub(cast, partial(kid.kid_state, conn))
    # HA-3: the same relay, reduced to the admin API's state; refreshed so jobs, disk and names keep up.
    counts = api.Counts(conn, config.media_dir)
    admin_hub = KidHub(
        cast,
        lambda cast_state: api.build_admin_state(conn, config, cast_state, counts.get()),
        unreachable=api.unreachable,
        initial=lambda: api.build_admin_state(conn, config, None, counts.get()),
        refresh_s=ADMIN_REFRESH_S,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.start()  # KA-7: relay the cast service's live state to kid pages
        admin_hub.start()  # HA-3: and to the admin API's event stream
        try:
            yield
        finally:
            await admin_hub.stop()
            await hub.stop()
            if owns_cast:
                await cast.aclose()
            if owns_oidc:
                await oidc.aclose()

    app = FastAPI(title="Tellybox", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.hub = hub
    app.state.admin_hub = admin_hub
    app.state.admin_counts = counts
    # A reverse proxy such as nginx on the same host may serve Tellybox over HTTPS. Trust
    # its X-Forwarded-For/-Proto, and only from 127.0.0.1: the login throttle then sees each
    # device's own address, and devices talking to the port directly can't spoof one.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["127.0.0.1"])
    app.add_middleware(LocaleMiddleware)  # NF-13: the browser's language, per request

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": config.version}

    # NF-3: the Chromecast can't authenticate, so the URL itself is the credential.
    # FileResponse handles Range (206) requests, which the Chromecast uses when seeking.
    @app.api_route("/media/{episode_id}/{expires_at}/{sig}.mp4", methods=["GET", "HEAD"])
    def media(episode_id: int, expires_at: int, sig: str) -> FileResponse:
        if not verify(config.secret, episode_id, expires_at, sig, clock.now()):
            raise HTTPException(status_code=404)
        episode = library.get_episode(conn, episode_id)
        if episode is None:
            raise HTTPException(status_code=404)
        path = resolve_media_file(config.media_dir, episode.file_path)
        if path is None:
            log.warning("media file unavailable episode_id=%s file_path=%r", episode_id, episode.file_path)
            raise HTTPException(status_code=404)
        # Hidden episodes are still served: the controller decides what plays.
        return FileResponse(path, media_type="video/mp4")

    # Kid app (KA-1..KA-9); no login (NF-1).
    app.include_router(kid.create_router(config, conn, cast, hub, resolve_media_file))

    # Admin JSON API (HA-1..HA-8); bearer tokens, no cookies.
    app.include_router(api.create_router(config, conn, clock, cast, admin_hub, counts))

    def static_file(name: str, **kwargs) -> FileResponse:
        path = static_dir / name
        if not path.is_file():
            log.error("kid app file missing: %s (frontend not built/installed?)", path)
            raise HTTPException(status_code=404)
        return FileResponse(path, **kwargs)

    @app.get("/")
    async def index() -> FileResponse:
        return static_file("index.html", headers={"Cache-Control": "no-cache"})

    # Browsers ask for /favicon.ico on their own; pages also link /static/favicon.svg.
    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return static_file("favicon.ico")

    @app.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        return static_file("manifest.webmanifest", media_type="application/manifest+json")

    # Admin pages (AD-1..AD-5); nothing in the kid app links here.
    mount_admin(app, AdminContext(config=config, conn=conn, clock=clock, cast=cast, ytdlp=ytdlp, oidc=oidc))

    # Revalidated on every load, so a deploy reaches open browsers without a forced refresh.
    app.mount("/static", NoCacheStaticFiles(directory=static_dir, check_dir=False), name="static")
    # The receiver page for self-hosters who don't use GitHub Pages; no login, like the kid app.
    # The TV must always fetch the current version, so nothing is cached.
    app.mount("/receiver", NoCacheStaticFiles(directory=RECEIVER_DIR, check_dir=False), name="receiver")
    return app
