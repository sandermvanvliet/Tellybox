"""AdminState (HA-2): the admin dashboard's live state in absolute units, as `docs/admin-api.md` describes it.

Static things (names, allowances, modes) come from the DB, live things (time used, who's watching) from the
cast service's state. Both are read on every reduction, so a renamed profile shows up at the next refresh.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from tellybox import api_tokens, jobs, library
from tellybox.config import Config
from tellybox.store import profile_policies

API_VERSION = 1
LAST_FIVE_S = 300  # KA-8: the kid app's "last five minutes"
COUNTS_TTL_S = 10.0

_GROUP_BLANK = {"remaining_s": None, "time_up": False, "last_five": False, "action": "continue", "reason": None,
                "grace_ends_at": None, "session_started_at": None, "session_elapsed_s": None}


def last_five(remaining_s: float | None) -> bool:
    return remaining_s is not None and remaining_s <= LAST_FIVE_S


class Counts:
    """Job and disk figures, refreshed at most every 10 s: the disk walk stats every media file."""

    def __init__(self, conn: sqlite3.Connection, media_dir: Path, ttl_s: float = COUNTS_TTL_S) -> None:
        self.conn = conn
        self.media_dir = media_dir
        self.ttl_s = ttl_s
        self._at: float | None = None
        self._value: dict = {}

    def invalidate(self) -> None:
        self._at = None

    def get(self) -> dict:
        now = time.monotonic()
        if self._at is None or now - self._at >= self.ttl_s:
            by_status = jobs.count_by_status(self.conn)
            try:
                free_bytes = shutil.disk_usage(self.media_dir).free
            except OSError:
                free_bytes = None
            self._value = {
                "jobs": {"queued": by_status.get("queued", 0),
                         "running": by_status.get("downloading", 0) + by_status.get("processing", 0),
                         "failed": by_status.get("failed", 0),
                         "held_ready": library.count_held_ready(self.conn)},
                "disk": {"media_bytes": library.disk_usage(self.conn, self.media_dir).total_bytes,
                         "free_bytes": free_bytes},
            }
            self._at = now
        return self._value


def _now_playing(conn: sqlite3.Connection, np: dict | None) -> dict | None:
    if not np:
        return None
    row = conn.execute("SELECT s.name FROM show s WHERE s.id = ?", (np.get("show_id"),)).fetchone()
    return {"episode_id": np["episode_id"], "show_id": np.get("show_id"), "title": np.get("title"),
            "show": row["name"] if row else None, "state": np.get("state"), "position_s": np.get("position_s"),
            "duration_s": np.get("duration_s"), "profile_ids": list(np.get("profile_ids") or [])}


def _group(cast_state: dict | None) -> dict:
    if not cast_state or not cast_state.get("timer"):
        return dict(_GROUP_BLANK)
    t = cast_state["timer"]
    return {
        "remaining_s": t.get("remaining_s"),
        "time_up": bool(cast_state.get("time_up")),
        "last_five": last_five(t.get("remaining_s")),
        "action": t.get("action", "continue"),
        "reason": t.get("reason"),
        "grace_ends_at": t.get("grace_deadline"),
        "session_started_at": t.get("session_started_at"),
        "session_elapsed_s": t.get("session_elapsed_s"),
    }


def _profiles(conn: sqlite3.Connection, cast_state: dict | None) -> list[dict]:
    """Profiles with resolved limits (A-23); *_source says whether each is inherit, custom or unlimited."""
    timers = {p["profile_id"]: p for p in ((cast_state or {}).get("timer") or {}).get("profiles", [])}
    policies = {p.profile_id: p for p in profile_policies(conn)}
    playing = ((cast_state or {}).get("now_playing") or {}).get("profile_ids") or []
    result = []
    for r in conn.execute("SELECT id, name, avatar, allowance_mode, max_session_mode FROM profile ORDER BY sort_order, id"):
        pol, t = policies[r["id"]], timers.get(r["id"])
        remaining_s = t.get("remaining_s") if t else None
        result.append({
            "id": r["id"], "name": r["name"], "avatar": r["avatar"],
            "allowance_s": None if pol.allowance_s is None else round(pol.allowance_s),
            "allowance_source": r["allowance_mode"],  # A-23: inherit|custom|unlimited
            "extra_s": t["extra_s"] if t else None,
            "used_s": t["used_s"] if t else None,
            "remaining_s": remaining_s,
            "unlimited": bool(t["unlimited"]) if t else False,
            "blocked": bool(t["blocked"]) if t else False,
            "mode": pol.mode.value,
            "max_session_s": None if pol.max_session_s is None else round(pol.max_session_s),
            "max_session_source": r["max_session_mode"],  # A-23: inherit|custom|unlimited
            "session_elapsed_s": t.get("session_elapsed_s") if t else None,
            "can_start": t.get("can_start") if t else None,
            "reason": t.get("reason") if t else None,
            "watching": r["id"] in playing,
            "last_five": last_five(remaining_s),
        })
    return result


def build_admin_state(conn: sqlite3.Connection, config: Config, cast_state: dict | None, counts: dict) -> dict:
    """The AdminState for a cast state; `None` (never seen one) gives the cold-start shape."""
    cs = cast_state or {}
    timer = cs.get("timer") or {}
    timer_profiles = timer.get("profiles") or []
    connection = cs.get("connection") or "unreachable"
    device = cs.get("device")
    return {
        "instance_id": api_tokens.instance_id(conn),
        "version": config.version,
        "api": API_VERSION,
        "day": {"date": timer_profiles[0].get("day") if timer_profiles else None,
                "resets_at": timer.get("next_reset")},
        "tv": {"connection": connection, "reachable": connection == "CONNECTED",
               "device": device.get("name") if device else None},
        "now_playing": _now_playing(conn, cs.get("now_playing")),
        "group": _group(cast_state),
        "profiles": _profiles(conn, cast_state),
        "jobs": counts["jobs"],
        "disk": counts["disk"],
    }


def unreachable(state: dict) -> dict:
    """The cast service can't be reached: keep the last known group and profiles, drop what's playing."""
    return {
        **state,
        "tv": {**state["tv"], "connection": "unreachable", "reachable": False},
        "now_playing": None,
        "profiles": [{**p, "watching": False} for p in state["profiles"]],
    }
