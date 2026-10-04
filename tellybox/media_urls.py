"""Signed, expiring media URLs (NF-3).

The Chromecast cannot authenticate, so each media URL carries an HMAC over the
episode id and expiry. The signature depends only on the shared secret
(Config.secret), so URLs stay valid across process restarts, and the episode id
can be read back from any URL regardless of its token (restart recovery, NF-7;
spike finding 7).

A URL for in-app playback (PB-7) also carries the watch session (``?s=<id>``), covered
by the signature: the web app serves it only while that session is open (WT-11), so
ending the session revokes the URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

MEDIA_TTL_S = 24 * 3600
_SIG_LEN = 22  # 132 bits of HMAC-SHA256

_PATH_RE = re.compile(r"^/media/(\d+)/(\d+)/([A-Za-z0-9_-]+)\.mp4$")


def _sign(secret: bytes, episode_id: int, expires_at: int, session_id: int | None = None) -> str:
    message = f"{episode_id}:{expires_at}" if session_id is None else f"{episode_id}:{expires_at}:{session_id}"
    mac = hmac.new(secret, message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()[:_SIG_LEN]


def media_path(secret: bytes, episode_id: int, expires_at: int, session_id: int | None = None) -> str:
    path = f"/media/{episode_id}/{expires_at}/{_sign(secret, episode_id, expires_at, session_id)}.mp4"
    return path if session_id is None else f"{path}?s={session_id}"


def media_url(
    base_url: str, secret: bytes, episode_id: int, now: datetime, ttl_s: int = MEDIA_TTL_S,
    session_id: int | None = None,
) -> str:
    expires_at = int(now.timestamp()) + ttl_s
    return base_url.rstrip("/") + media_path(secret, episode_id, expires_at, session_id)


def verify(
    secret: bytes, episode_id: int, expires_at: int, sig: str, now: datetime, session_id: int | None = None
) -> bool:
    if expires_at < now.timestamp():
        return False
    return hmac.compare_digest(
        _sign(secret, episode_id, expires_at, session_id).encode(), sig.encode("utf-8", "surrogateescape")
    )


def session_id_from_query(query: str | None) -> int | None:
    """The watch session id in a session-scoped URL's ``s`` query parameter, else None."""
    values = parse_qs(query or "").get("s")
    if not values or len(values) != 1 or not values[0].isdigit() or len(values[0]) > 18:
        return None
    return int(values[0])


def episode_id_from_url(url: str | None) -> int | None:
    """Episode id if `url` points at one of our media paths, else None (WT-9: ignore other casts)."""
    if not url:
        return None
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    m = _PATH_RE.match(path)
    return int(m.group(1)) if m else None
