"""Persistence for the cast controller: timer state, history, positions, devices.

Only the cast service writes these tables (it is the single owner of the timer);
the web app reads them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tellybox import sponsorblock
from tellybox.cast.device import DeviceInfo
from tellybox.db import from_db, to_db
from tellybox.timer import CountingMode, DayUsage, ProfilePolicy, TimerSettings

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- settings


def timer_settings(conn: sqlite3.Connection, tz: ZoneInfo) -> TimerSettings:
    row = conn.execute("SELECT reset_time, grace_cap_min, session_break_min FROM settings WHERE id = 1").fetchone()
    return TimerSettings(
        reset_time=time.fromisoformat(row["reset_time"]),
        tz=tz,
        grace_cap_s=row["grace_cap_min"] * 60.0,
        session_break_s=row["session_break_min"] * 60.0,
    )


def receiver_app_id(conn: sqlite3.Connection) -> str | None:
    """The Tellybox receiver's Cast app id (v7, CR-1); None or empty means the Default Media Receiver only."""
    row = conn.execute("SELECT receiver_app_id FROM settings WHERE id = 1").fetchone()
    return (row["receiver_app_id"] or "").strip().upper() or None if row else None


def profile_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM profile ORDER BY id")]


def _resolve_allowance_s(
    conn: sqlite3.Connection, allowance_mode: str, profile_allowance_min: int | None
) -> float | None:
    """Resolve the effective allowance for one profile.

    inherit -> settings.default_allowance_min (in minutes)
    custom -> profile.daily_allowance_min (in minutes)
    unlimited -> None

    A-23: per-profile limits.
    """
    if allowance_mode == "unlimited":
        return None
    if allowance_mode == "custom":
        return profile_allowance_min * 60.0 if profile_allowance_min is not None else None
    # inherit
    row = conn.execute("SELECT default_allowance_min FROM settings WHERE id = 1").fetchone()
    default_min = row["default_allowance_min"] if row else 60
    return default_min * 60.0


def _resolve_max_session_s(
    conn: sqlite3.Connection, max_session_mode: str, profile_max_session_min: int | None
) -> float | None:
    """Resolve the effective max session length for one profile.

    inherit -> settings.default_max_session_min (in minutes)
    custom -> profile.max_session_min (in minutes)
    unlimited -> None

    A-23: per-profile limits.
    """
    if max_session_mode == "unlimited":
        return None
    if max_session_mode == "custom":
        return profile_max_session_min * 60.0 if profile_max_session_min is not None else None
    # inherit
    row = conn.execute("SELECT default_max_session_min FROM settings WHERE id = 1").fetchone()
    default_min = row["default_max_session_min"] if row else 90
    return default_min * 60.0


def effective_allowance_s(conn: sqlite3.Connection, profile_id: int) -> float | None:
    """Get the effective allowance (in seconds) for a profile, resolving inherit/custom/unlimited.

    A-23: per-profile limits.
    """
    row = conn.execute(
        "SELECT allowance_mode, daily_allowance_min FROM profile WHERE id = ?",
        (profile_id,)
    ).fetchone()
    if not row:
        return None
    return _resolve_allowance_s(conn, row["allowance_mode"], row["daily_allowance_min"])


def effective_max_session_s(conn: sqlite3.Connection, profile_id: int) -> float | None:
    """Get the effective max session length (in seconds) for a profile, resolving inherit/custom/unlimited.

    A-23: per-profile limits.
    """
    row = conn.execute(
        "SELECT max_session_mode, max_session_min FROM profile WHERE id = ?",
        (profile_id,)
    ).fetchone()
    if not row:
        return None
    return _resolve_max_session_s(conn, row["max_session_mode"], row["max_session_min"])


def profile_policies(conn: sqlite3.Connection, ids: list[int] | None = None) -> list[ProfilePolicy]:
    rows = conn.execute(
        "SELECT id, allowance_mode, daily_allowance_min, counting_mode, max_session_mode, max_session_min FROM profile ORDER BY id"
    ).fetchall()
    return [
        ProfilePolicy(
            profile_id=r["id"],
            allowance_s=_resolve_allowance_s(conn, r["allowance_mode"], r["daily_allowance_min"]),
            mode=CountingMode(r["counting_mode"]),
            max_session_s=_resolve_max_session_s(conn, r["max_session_mode"], r["max_session_min"]),
        )
        for r in rows
        if ids is None or r["id"] in ids
    ]


# --------------------------------------------------------------------------- timer (WT-8)


def load_usages(conn: sqlite3.Connection, day: date) -> list[DayUsage]:
    rows = conn.execute("SELECT * FROM daily_usage WHERE day = ?", (day.isoformat(),)).fetchall()
    return [
        DayUsage(
            profile_id=r["profile_id"],
            day=day,
            used_s=r["seconds_used"],
            extra_s=r["extra_min"] * 60.0,
            unlimited=bool(r["unlimited"]),
            blocked=bool(r["blocked"]),
        )
        for r in rows
    ]


def save_usages(conn: sqlite3.Connection, usages: list[DayUsage], now: datetime) -> None:
    for u in usages:
        conn.execute(
            """INSERT INTO daily_usage (profile_id, day, seconds_used, extra_min, unlimited, blocked, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (profile_id, day) DO UPDATE SET
                 seconds_used = excluded.seconds_used, extra_min = excluded.extra_min,
                 unlimited = excluded.unlimited, blocked = excluded.blocked, updated_at = excluded.updated_at""",
            (u.profile_id, u.day.isoformat(), u.used_s, round(u.extra_s / 60), int(u.unlimited), int(u.blocked), to_db(now)),
        )


def load_timer_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT state_json FROM timer_state WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


def save_timer_snapshot(conn: sqlite3.Connection, snapshot: dict, now: datetime) -> None:
    conn.execute(
        """INSERT INTO timer_state (id, state_json, updated_at) VALUES (1, ?, ?)
           ON CONFLICT (id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at""",
        (json.dumps(snapshot), to_db(now)),
    )


def log_override(
    conn: sqlite3.Connection,
    profile_id: int | None,
    day: date,
    kind: str,
    value: int | None,
    now: datetime,
    source: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO override_log (profile_id, day, kind, value, created_at, source) VALUES (?, ?, ?, ?, ?, ?)",
        (profile_id, day.isoformat(), kind, value, to_db(now), source),
    )


# --------------------------------------------------------------------------- history (AD-4)


@dataclass(frozen=True)
class OpenSession:
    id: int
    episode_id: int | None
    started_at: datetime
    last_heartbeat_at: datetime | None
    cast_session_id: str | None
    profile_ids: list[int]


def open_watch_session(
    conn: sqlite3.Connection, episode_id: int, profiles: list[int], now: datetime, cast_session_id: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO watch_session (episode_id, started_at, last_heartbeat_at, cast_session_id) VALUES (?, ?, ?, ?)",
        (episode_id, to_db(now), to_db(now), cast_session_id),
    )
    session_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO watch_session_profile (watch_session_id, profile_id) VALUES (?, ?)",
        [(session_id, p) for p in profiles],
    )
    return session_id


def set_cast_session_id(conn: sqlite3.Connection, session_id: int, cast_session_id: str) -> None:
    conn.execute("UPDATE watch_session SET cast_session_id = ? WHERE id = ?", (cast_session_id, session_id))


def heartbeat_watch_session(conn: sqlite3.Connection, session_id: int, add_seconds: float, now: datetime) -> None:
    conn.execute(
        "UPDATE watch_session SET seconds_counted = seconds_counted + ?, last_heartbeat_at = ? WHERE id = ?",
        (add_seconds, to_db(now), session_id),
    )


def close_watch_session(
    conn: sqlite3.Connection, session_id: int, reason: str, ended_at: datetime, add_seconds: float = 0.0
) -> None:
    conn.execute(
        """UPDATE watch_session SET seconds_counted = seconds_counted + ?, ended_at = ?, end_reason = ?,
           last_heartbeat_at = ? WHERE id = ? AND ended_at IS NULL""",
        (add_seconds, to_db(ended_at), reason, to_db(ended_at), session_id),
    )


def open_watch_sessions(conn: sqlite3.Connection) -> list[OpenSession]:
    rows = conn.execute("SELECT * FROM watch_session WHERE ended_at IS NULL ORDER BY started_at, id").fetchall()
    result = []
    for r in rows:
        profiles = [p[0] for p in conn.execute(
            "SELECT profile_id FROM watch_session_profile WHERE watch_session_id = ? ORDER BY profile_id", (r["id"],)
        )]
        result.append(OpenSession(
            id=r["id"],
            episode_id=r["episode_id"],
            started_at=from_db(r["started_at"]),
            last_heartbeat_at=from_db(r["last_heartbeat_at"]),
            cast_session_id=r["cast_session_id"],
            profile_ids=profiles,
        ))
    return result


# --------------------------------------------------------------------------- positions (PB-4)

FINISHED_FRACTION = 0.95


def is_finished(position_s: float, duration_s: float | None) -> bool:
    return bool(duration_s) and position_s >= FINISHED_FRACTION * duration_s


def save_position(
    conn: sqlite3.Connection, profiles: list[int], episode_id: int, position_s: float, finished: bool, now: datetime
) -> None:
    for p in profiles:
        conn.execute(
            """INSERT INTO playback_position (profile_id, episode_id, position_s, finished, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (profile_id, episode_id) DO UPDATE SET
                 position_s = excluded.position_s, finished = excluded.finished, updated_at = excluded.updated_at""",
            (p, episode_id, position_s, int(finished), to_db(now)),
        )


def apply_position_shifts(conn: sqlite3.Connection, now: datetime) -> int:
    """SB-3: move saved positions after the worker replaced an episode's file; returns the rows applied.

    The worker only queues `position_shift` rows; this is the one writer of playback_position.
    Rows are applied in id order, so several replacements in a row compose. updated_at is
    kept: a replaced file doesn't move the episode up in continue watching (PB-4).
    """
    if conn.execute("SELECT 1 FROM position_shift LIMIT 1").fetchone() is None:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        shifts = conn.execute("SELECT id, episode_id, old_cuts_json, new_cuts_json FROM position_shift ORDER BY id").fetchall()
        for shift in shifts:
            old = sponsorblock.loads(shift["old_cuts_json"])
            new = sponsorblock.loads(shift["new_cuts_json"])
            row = conn.execute("SELECT duration_s FROM episode WHERE id = ?", (shift["episode_id"],)).fetchone()
            duration_s = row["duration_s"] if row else None
            for pos in conn.execute(
                "SELECT profile_id, position_s FROM playback_position WHERE episode_id = ?", (shift["episode_id"],)
            ).fetchall():
                moved = sponsorblock.remap_position(pos["position_s"], old, new)
                if duration_s:
                    moved = min(moved, duration_s)
                conn.execute(
                    "UPDATE playback_position SET position_s = ? WHERE profile_id = ? AND episode_id = ?",
                    (moved, pos["profile_id"], shift["episode_id"]),
                )
            conn.execute("DELETE FROM position_shift WHERE id = ?", (shift["id"],))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(shifts)


def group_position(conn: sqlite3.Connection, profile_ids: list[int], episode_id: int) -> float | None:
    """Where a group resumes (PB-4): the most recently updated unfinished position among its members."""
    marks = ",".join("?" * len(profile_ids))
    row = conn.execute(
        f"""SELECT position_s FROM playback_position
            WHERE episode_id = ? AND finished = 0 AND profile_id IN ({marks})
            ORDER BY updated_at DESC, profile_id LIMIT 1""",
        (episode_id, *profile_ids),
    ).fetchone()
    return row["position_s"] if row else None


def get_position(conn: sqlite3.Connection, profile_id: int, episode_id: int) -> tuple[float, bool] | None:
    row = conn.execute(
        "SELECT position_s, finished FROM playback_position WHERE profile_id = ? AND episode_id = ?",
        (profile_id, episode_id),
    ).fetchone()
    return (row["position_s"], bool(row["finished"])) if row else None


# --------------------------------------------------------------------------- devices (PB-1)


def selected_device(conn: sqlite3.Connection) -> DeviceInfo | None:
    row = conn.execute("SELECT * FROM cast_device WHERE selected = 1").fetchone()
    if not row:
        return None
    return DeviceInfo(uuid=row["uuid"], name=row["name"], host=row["host"], port=row["port"], model=row["model"])


def device_info(conn: sqlite3.Connection, uuid: str | None) -> DeviceInfo | None:
    """A remembered Chromecast by uuid; None when unknown."""
    row = conn.execute("SELECT * FROM cast_device WHERE uuid = ?", (uuid,)).fetchone() if uuid else None
    if not row:
        return None
    return DeviceInfo(uuid=row["uuid"], name=row["name"], host=row["host"], port=row["port"], model=row["model"])


def target_device(conn: sqlite3.Connection, profile_id: int) -> DeviceInfo | None:
    """Where this profile's picks play (PB-6): its own default TV when set and known, else the global selected one."""
    row = conn.execute("SELECT cast_device_uuid FROM profile WHERE id = ?", (profile_id,)).fetchone()
    return (device_info(conn, row["cast_device_uuid"]) if row else None) or selected_device(conn)


def remember_devices(conn: sqlite3.Connection, devices: list[DeviceInfo], now: datetime) -> None:
    for d in devices:
        conn.execute(
            """INSERT INTO cast_device (uuid, name, host, port, model, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (uuid) DO UPDATE SET name = excluded.name, host = excluded.host, port = excluded.port,
                 model = excluded.model, last_seen_at = excluded.last_seen_at""",
            (d.uuid, d.name, d.host, d.port, d.model, to_db(now)),
        )


def select_device(conn: sqlite3.Connection, uuid: str) -> None:
    conn.execute("BEGIN")
    conn.execute("UPDATE cast_device SET selected = 0")
    conn.execute("UPDATE cast_device SET selected = 1 WHERE uuid = ?", (uuid,))
    conn.execute("COMMIT")


# --------------------------------------------------------------------------- receiver events (CR-6)

RECEIVER_FAILURE_KINDS = ("launch_failed", "refused", "lost", "recover_failed", "page_error")


def record_receiver_event(
    conn: sqlite3.Connection,
    now: datetime,
    kind: str,
    detail: str | None = None,
    *,
    duration_ms: int | None = None,
    episode_id: int | None = None,
) -> None:
    """Log a receiver event. Never raises: a failed write must not break playback."""
    try:
        conn.execute(
            "INSERT INTO receiver_event (at, kind, detail, duration_ms, episode_id) VALUES (?, ?, ?, ?, ?)",
            (to_db(now), kind, detail, duration_ms, episode_id),
        )
    except sqlite3.Error:
        log.warning("could not record receiver event %s", kind, exc_info=True)


def receiver_summary(conn: sqlite3.Connection, now: datetime) -> dict:
    """The last receiver failure, and the failures and launches of the last 24 h (for the cast state)."""
    since = to_db(now - timedelta(hours=24))
    marks = ",".join("?" * len(RECEIVER_FAILURE_KINDS))
    last = conn.execute(
        f"SELECT kind, detail, at FROM receiver_event WHERE kind IN ({marks}) ORDER BY at DESC, id DESC LIMIT 1",
        RECEIVER_FAILURE_KINDS,
    ).fetchone()
    failures = conn.execute(
        f"SELECT COUNT(*) FROM receiver_event WHERE at >= ? AND kind IN ({marks})", (since, *RECEIVER_FAILURE_KINDS)
    ).fetchone()[0]
    launches = conn.execute(
        "SELECT COUNT(*) FROM receiver_event WHERE at >= ? AND kind = 'launch_ok'", (since,)
    ).fetchone()[0]
    return {
        "last_failure": {"kind": last["kind"], "detail": last["detail"], "at": last["at"]} if last else None,
        "failures_24h": failures,
        "launches_24h": launches,
    }
