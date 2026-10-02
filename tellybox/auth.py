"""Admin sign-in (AD-1, NF-2): a password from the environment or chosen in the browser with a setup code, cookie sessions, login throttling.

The password's argon2id hash is stored in `settings` at startup; when the configured
password no longer matches it, the hash is replaced and every session ends. Sessions are
random tokens in a cookie; only their SHA-256 is stored, with a sliding 30-day expiry.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from tellybox.db import from_db, to_db
from tellybox.library import _transaction

log = logging.getLogger(__name__)

SESSION_COOKIE = "tb_admin"
SESSION_IDLE = timedelta(days=30)
THROTTLE_MIN_S = 1.0
THROTTLE_MAX_S = 60.0

_hasher = PasswordHasher()  # argon2id


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- password


def install_password(conn: sqlite3.Connection, password: str | None) -> None:
    """Apply the environment's password at startup (DP-4).

    The environment always wins: its hash is stored with source 'env', and a changed password ends
    all sessions. Without one, a password chosen in the browser (source 'ui') is kept; a leftover
    'env' password is removed (ending sessions), so the setup page opens again.
    """
    with _transaction(conn):
        stored, source = conn.execute(
            "SELECT admin_password_hash, admin_password_source FROM settings WHERE id = 1"
        ).fetchone()
        if password is None:
            if stored is not None and source != "ui":
                conn.execute(
                    "UPDATE settings SET admin_password_hash = NULL, admin_password_source = NULL WHERE id = 1"
                )
                conn.execute("DELETE FROM admin_session")
                log.info("admin password removed; sign-in needs a new one and all sessions ended")
            return
        if stored is not None and source == "env" and _matches(stored, password):
            if _hasher.check_needs_rehash(stored):
                conn.execute("UPDATE settings SET admin_password_hash = ? WHERE id = 1", (_hasher.hash(password),))
            return
        conn.execute(
            """UPDATE settings SET admin_password_hash = ?, admin_password_source = 'env',
               admin_setup_code_hash = NULL, admin_setup_code_created_at = NULL WHERE id = 1""",
            (_hasher.hash(password),),
        )
        ended = conn.execute("DELETE FROM admin_session").rowcount
        log.info("admin password set from the environment; %d existing session(s) ended", ended)


def _matches(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def is_locked(conn: sqlite3.Connection) -> bool:
    """True when no admin password is configured."""
    return conn.execute("SELECT admin_password_hash FROM settings WHERE id = 1").fetchone()[0] is None


# --------------------------------------------------------------------------- first-run setup code (DP-4)

# Base32 without the look-alikes 0/O and 1/I/L.
SETUP_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SETUP_MIN_PASSWORD = 8


def _normalise_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def new_setup_code(conn: sqlite3.Connection, now: datetime) -> str:
    """A fresh setup code ('XXXX-XXXX'); replaces any earlier one. Only its SHA-256 is stored."""
    raw = "".join(secrets.choice(SETUP_ALPHABET) for _ in range(8))
    conn.execute(
        "UPDATE settings SET admin_setup_code_hash = ?, admin_setup_code_created_at = ? WHERE id = 1",
        (_token_hash(raw), to_db(now)),
    )
    return f"{raw[:4]}-{raw[4:]}"


def check_setup_code(conn: sqlite3.Connection, code: str) -> bool:
    stored = conn.execute("SELECT admin_setup_code_hash FROM settings WHERE id = 1").fetchone()[0]
    if stored is None:
        return False
    return secrets.compare_digest(stored, _token_hash(_normalise_code(code)))


def set_ui_password(conn: sqlite3.Connection, password: str) -> bool:
    """Store a password chosen in the browser, spend the setup code and end all sessions.

    Refuses (False) when a password already exists, so a code can't overwrite one.
    """
    with _transaction(conn):
        if conn.execute("SELECT admin_password_hash FROM settings WHERE id = 1").fetchone()[0] is not None:
            return False
        conn.execute(
            """UPDATE settings SET admin_password_hash = ?, admin_password_source = 'ui',
               admin_setup_code_hash = NULL, admin_setup_code_created_at = NULL WHERE id = 1""",
            (_hasher.hash(password),),
        )
        conn.execute("DELETE FROM admin_session")
    return True


def reset_ui_password(conn: sqlite3.Connection) -> bool:
    """Forget a password chosen in the browser and end all sessions. False if there was none."""
    with _transaction(conn):
        source = conn.execute("SELECT admin_password_source FROM settings WHERE id = 1").fetchone()[0]
        if source != "ui":
            return False
        conn.execute(
            "UPDATE settings SET admin_password_hash = NULL, admin_password_source = NULL WHERE id = 1"
        )
        conn.execute("DELETE FROM admin_session")
    return True


def check_password(conn: sqlite3.Connection, password: str) -> bool:
    stored = conn.execute("SELECT admin_password_hash FROM settings WHERE id = 1").fetchone()[0]
    return stored is not None and _matches(stored, password)


# --------------------------------------------------------------------------- sessions


def create_session(conn: sqlite3.Connection, now: datetime) -> str:
    """New session; returns the cookie token (never stored)."""
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO admin_session (token_hash, created_at, last_used_at) VALUES (?, ?, ?)",
        (_token_hash(token), to_db(now), to_db(now)),
    )
    return token


def touch_session(conn: sqlite3.Connection, token: str | None, now: datetime) -> bool:
    """True if the token is a live session; each use restarts the 30-day clock. Expired sessions are removed."""
    if not token:
        return False
    h = _token_hash(token)
    row = conn.execute("SELECT last_used_at FROM admin_session WHERE token_hash = ?", (h,)).fetchone()
    if row is None:
        return False
    if now - from_db(row["last_used_at"]) >= SESSION_IDLE:
        conn.execute("DELETE FROM admin_session WHERE token_hash = ?", (h,))
        return False
    conn.execute("UPDATE admin_session SET last_used_at = ? WHERE token_hash = ?", (to_db(now), h))
    return True


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM admin_session WHERE token_hash = ?", (_token_hash(token),))


def purge_expired_sessions(conn: sqlite3.Connection, now: datetime) -> int:
    return conn.execute(
        "DELETE FROM admin_session WHERE last_used_at <= ?", (to_db(now - SESSION_IDLE),)
    ).rowcount


# --------------------------------------------------------------------------- login throttling


def throttle_wait_s(conn: sqlite3.Connection, address: str, now: datetime) -> float:
    """Seconds this address must still wait before trying again; 0 if it may try now."""
    row = conn.execute("SELECT next_allowed_at FROM login_throttle WHERE address = ?", (address,)).fetchone()
    if row is None:
        return 0.0
    return max(0.0, (from_db(row["next_allowed_at"]) - now).total_seconds())


def record_failure(conn: sqlite3.Connection, address: str, now: datetime) -> float:
    """Count a failed sign-in; returns the new wait: 1, 2, 4, ... capped at 60 s."""
    with _transaction(conn):
        row = conn.execute("SELECT failures FROM login_throttle WHERE address = ?", (address,)).fetchone()
        failures = (row["failures"] if row else 0) + 1
        wait = min(THROTTLE_MAX_S, THROTTLE_MIN_S * 2 ** (failures - 1))
        conn.execute(
            """INSERT INTO login_throttle (address, failures, next_allowed_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (address) DO UPDATE SET failures = excluded.failures,
                 next_allowed_at = excluded.next_allowed_at, updated_at = excluded.updated_at""",
            (address, failures, to_db(now + timedelta(seconds=wait)), to_db(now)),
        )
    return wait


def clear_failures(conn: sqlite3.Connection, address: str) -> None:
    conn.execute("DELETE FROM login_throttle WHERE address = ?", (address,))
