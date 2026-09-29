"""API tokens for the admin API (HA-1, HA-6): long-lived bearer tokens for Home Assistant and the like.

A token's secret is `tbx_` plus 43 url-safe characters. It is shown once at creation; only its
SHA-256 is stored, as with admin sessions. Scopes are `read` (state and events) and `control`
(overrides), and `control` implies `read`. Tokens are independent of the admin password (A-16):
changing the password doesn't revoke them. Revoked tokens stay listed.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from tellybox.db import from_db, to_db

PREFIX = "tbx_"
READ = "read"
CONTROL = "control"
SCOPES = (READ, CONTROL)
NAME_MAX = 40
LAST_USED_RESOLUTION = timedelta(minutes=1)  # write last_used_at at most this often


@dataclass(frozen=True)
class ApiToken:
    id: int
    name: str
    scopes: frozenset[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def allows(self, scope: str) -> bool:
        """`control` implies `read`."""
        return scope in self.scopes or (scope == READ and CONTROL in self.scopes)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _normalise_scopes(scopes) -> frozenset[str]:
    wanted = frozenset(scopes)
    if not wanted or not wanted <= set(SCOPES):
        raise ValueError(f"scopes must be a non-empty subset of {SCOPES}")
    return wanted | {READ}


def _row(row: sqlite3.Row) -> ApiToken:
    return ApiToken(
        id=row["id"],
        name=row["name"],
        scopes=frozenset(row["scopes"].split()),
        created_at=from_db(row["created_at"]),
        last_used_at=from_db(row["last_used_at"]),
        revoked_at=from_db(row["revoked_at"]),
    )


def create_token(conn: sqlite3.Connection, name: str, scopes, now: datetime) -> tuple[int, str]:
    """New token; returns `(id, secret)`. The secret is never stored, so show it now or never."""
    name = name.strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise ValueError(f"name must be 1..{NAME_MAX} characters")
    stored_scopes = " ".join(s for s in SCOPES if s in _normalise_scopes(scopes))
    secret = PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO api_token (name, token_hash, scopes, created_at) VALUES (?, ?, ?, ?)",
        (name, _hash(secret), stored_scopes, to_db(now)),
    )
    return cur.lastrowid, secret


def list_tokens(conn: sqlite3.Connection) -> list[ApiToken]:
    """Every token, live ones first, newest first."""
    rows = conn.execute(
        "SELECT * FROM api_token ORDER BY revoked_at IS NOT NULL, created_at DESC, id DESC"
    ).fetchall()
    return [_row(r) for r in rows]


def get_token(conn: sqlite3.Connection, token_id: int) -> ApiToken | None:
    row = conn.execute("SELECT * FROM api_token WHERE id = ?", (token_id,)).fetchone()
    return _row(row) if row else None


def revoke_token(conn: sqlite3.Connection, token_id: int, now: datetime) -> bool:
    """True if a live token was revoked; revoking twice keeps the first time."""
    return conn.execute(
        "UPDATE api_token SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (to_db(now), token_id)
    ).rowcount == 1


def authenticate(conn: sqlite3.Connection, secret: str | None, now: datetime) -> ApiToken | None:
    """The live token for this secret, or None. Records the use, at most once a minute."""
    if not secret or not secret.startswith(PREFIX):
        return None
    row = conn.execute("SELECT * FROM api_token WHERE token_hash = ?", (_hash(secret),)).fetchone()
    if row is None:
        return None
    token = _row(row)
    if token.revoked:
        return None
    if token.last_used_at is None or now - token.last_used_at >= LAST_USED_RESOLUTION:
        conn.execute("UPDATE api_token SET last_used_at = ? WHERE id = ?", (to_db(now), token.id))
    return token


def instance_id(conn: sqlite3.Connection) -> str:
    """This installation's stable id (HA-6), set by migration 007."""
    return conn.execute("SELECT instance_id FROM settings WHERE id = 1").fetchone()[0]
