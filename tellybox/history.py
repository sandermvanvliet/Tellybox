"""History queries for the admin History page (AD-4).

Pure functions: given a connection and the reference time/zone/reset time, they
return read-only dataclasses grouped by the timer day (WT-1) that watch_session
rows and override_log rows belong to. The admin page renders them; nothing here
touches sqlite3.Row objects outside this module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tellybox.db import from_db, to_db
from tellybox.i18n import N_, _
from tellybox.timer.day import day_for

HISTORY_DAYS = 21  # AD-5: history is purged after this many days
WATCHING_NOW_LABEL = N_("watching now")

# Keyed by watch_session.end_reason (tellybox.cast.controller.EndReason values); translated
# into the current language by history_days().
END_REASON_LABELS: dict[str, str] = {
    "finished": N_("finished"),
    "replaced": N_("replaced by another pick"),
    "stopped": N_("stopped"),
    "parent_stop": N_("stopped by a parent"),
    "time_up": N_("time was up"),
    "blocked": N_("blocked"),
    "taken_over": N_("taken over by another cast"),
    "disconnected": N_("connection lost"),
    "restart": N_("interrupted by a restart"),
    "load_failed": N_("failed to load"),
}


@dataclass(frozen=True)
class HistoryProfile:
    id: int
    name: str
    avatar: str | None
    picture_path: str | None


@dataclass(frozen=True)
class HistoryEpisode:
    watch_session_id: int
    episode_id: int | None
    title: str  # "deleted episode" once the episode is gone
    show_name: str | None
    started_at: datetime
    ended_at: datetime | None
    minutes: int  # seconds_counted, rounded
    end_reason_label: str  # WATCHING_NOW_LABEL for an open session
    profiles: tuple[HistoryProfile, ...] = ()  # who watched, in the admin's order
    target: str = "tv"  # PB-8, AD-4: 'tv' or 'device'
    device_label: str | None = None  # a short browser label such as "iPhone Safari"


@dataclass(frozen=True)
class HistoryOverride:
    profile_id: int | None  # None = everyone
    kind: str
    value: int | None
    created_at: datetime
    profile: HistoryProfile | None = None  # None = everyone (or a profile that has been deleted)
    source: str | None = None  # API token name (HA-7); None = the admin pages


@dataclass(frozen=True)
class HistoryDay:
    day: date  # local watch day (WT-1)
    episodes: list[HistoryEpisode] = field(default_factory=list)
    overrides: list[HistoryOverride] = field(default_factory=list)


def _end_reason_label(end_reason: str | None, ended_at: datetime | None) -> str:
    if ended_at is None:
        return _(WATCHING_NOW_LABEL)
    if end_reason is None:
        return "–"
    label = END_REASON_LABELS.get(end_reason)
    return _(label) if label else end_reason


def _profiles(conn: sqlite3.Connection) -> dict[int, HistoryProfile]:
    rows = conn.execute("SELECT id, name, avatar, picture_path FROM profile ORDER BY sort_order, id").fetchall()
    return {r["id"]: HistoryProfile(r["id"], r["name"], r["avatar"], r["picture_path"]) for r in rows}


def history_days(
    conn: sqlite3.Connection, now: datetime, tz: ZoneInfo, reset_time: time, *, days: int = HISTORY_DAYS,
    profile_id: int | None = None,
) -> list[HistoryDay]:
    """The last `days` watch days (AD-4), newest first. Days with nothing in them are included empty.

    `profile_id` keeps only the sessions that profile was part of and its overrides (plus those
    for everyone); each session still lists everyone who watched.
    """
    profiles = _profiles(conn)
    order = {pid: i for i, pid in enumerate(profiles)}
    watchers: dict[int, list[int]] = {}
    for r in conn.execute("SELECT watch_session_id, profile_id FROM watch_session_profile"):
        watchers.setdefault(r["watch_session_id"], []).append(r["profile_id"])
    latest_day = day_for(now, reset_time, tz)
    by_day: dict[date, HistoryDay] = {
        latest_day - timedelta(days=i): HistoryDay(day=latest_day - timedelta(days=i)) for i in range(days)
    }
    earliest_day = latest_day - timedelta(days=days - 1)

    # Coarse SQL prefilter (a couple of days' slack for zone/reset edges); day_for() decides exactly.
    rows = conn.execute(
        """SELECT ws.id, ws.episode_id, ws.started_at, ws.ended_at, ws.seconds_counted, ws.end_reason, ws.target, ws.device_label,
                  e.title AS episode_title, s.name AS show_name
           FROM watch_session ws
           LEFT JOIN episode e ON e.id = ws.episode_id
           LEFT JOIN show s ON s.id = e.show_id
           WHERE ws.started_at >= ?
           ORDER BY ws.started_at""",
        (to_db(now - timedelta(days=days + 2)),),
    ).fetchall()
    for r in rows:
        members = sorted((p for p in watchers.get(r["id"], []) if p in profiles), key=order.__getitem__)
        if profile_id is not None and profile_id not in members:
            continue
        started_at = from_db(r["started_at"])
        d = day_for(started_at, reset_time, tz)
        bucket = by_day.get(d)
        if bucket is None:
            continue
        ended_at = from_db(r["ended_at"])
        bucket.episodes.append(HistoryEpisode(
            watch_session_id=r["id"],
            episode_id=r["episode_id"],
            title=r["episode_title"] or _("deleted episode"),
            show_name=r["show_name"],
            started_at=started_at,
            ended_at=ended_at,
            minutes=round(r["seconds_counted"] / 60),
            end_reason_label=_end_reason_label(r["end_reason"], ended_at),
            profiles=tuple(profiles[p] for p in members),
            target=r["target"],
            device_label=r["device_label"],
        ))

    orows = conn.execute(
        "SELECT profile_id, day, kind, value, created_at, source FROM override_log WHERE day >= ? ORDER BY created_at",
        (earliest_day.isoformat(),),
    ).fetchall()
    for r in orows:
        if profile_id is not None and r["profile_id"] not in (None, profile_id):
            continue
        d = date.fromisoformat(r["day"])
        bucket = by_day.get(d)
        if bucket is None:
            continue
        bucket.overrides.append(HistoryOverride(
            profile_id=r["profile_id"], kind=r["kind"], value=r["value"], created_at=from_db(r["created_at"]),
            profile=profiles.get(r["profile_id"]), source=r["source"],
        ))

    return [by_day[d] for d in sorted(by_day, reverse=True)]


# --- Admin API usage history (HA-12, A-38; step 20) -----------------------------------------------
# The contract for `GET /api/admin/history` (docs/admin-api.md, "History"). `usage_history` is the query.


@dataclass(frozen=True)
class UsageDay:
    date: date  # a timer day (WT-1), not a calendar day
    used_s: int  # daily_usage.seconds_used, rounded
    extra_s: int  # daily_usage.extra_min * 60
    unlimited: bool
    blocked: bool


@dataclass(frozen=True)
class LastWatched:
    episode_id: int | None  # None once the episode is deleted
    title: str | None
    show: str | None
    started_at: datetime
    ended_at: datetime | None  # None while the session is open
    target: str  # 'tv' or 'device'


@dataclass(frozen=True)
class ProfileUsage:
    id: int
    name: str
    days: tuple[UsageDay, ...]  # exactly `days` long, newest first, today first, zeros for a day without a row
    last_watched: LastWatched | None


@dataclass(frozen=True)
class UsageHistory:
    today: date  # the current timer day
    days: int
    profiles: tuple[ProfileUsage, ...]  # in the admin's order (sort_order, id)


def usage_history(
    conn: sqlite3.Connection, now: datetime, tz: ZoneInfo, reset_time: time, days: int = 7,
    profile_ids: list[int] | None = None,
) -> UsageHistory:
    """The daily totals and the last watched episode per profile (HA-12).

    `days` must be 1..HISTORY_DAYS (ValueError otherwise); an unknown id in `profile_ids` raises ValueError.
    Implemented by step 20, task T1."""
    raise NotImplementedError
