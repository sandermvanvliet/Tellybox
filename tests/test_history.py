"""History queries for the admin History page (AD-4, AD-5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tellybox import db, library, store
from tellybox.cast.controller import EndReason
from tellybox.history import WATCHING_NOW_LABEL, history_days

AMS = ZoneInfo("Europe/Amsterdam")
FOUR = time(4, 0)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)  # 14:00 CEST


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def profile_id(conn) -> int:
    return conn.execute("SELECT id FROM profile ORDER BY id LIMIT 1").fetchone()[0]


@pytest.fixture
def episode_id(conn) -> int:
    show_id = library.create_show(conn, "Bluey", now=NOW)
    return library.add_episode(conn, show_id, "Hospital", "eps/hospital.mp4", now=NOW, duration_s=420.0)


def open_close(conn, episode_id, profile_id, started_at, ended_at, *, reason=EndReason.FINISHED, seconds=None):
    session_id = store.open_watch_session(conn, episode_id, [profile_id], started_at)
    counted = seconds if seconds is not None else (ended_at - started_at).total_seconds()
    store.close_watch_session(conn, session_id, reason, ended_at, counted)
    return session_id


def test_session_before_reset_belongs_to_previous_day(conn, episode_id, profile_id):
    # 03:00 local (CEST) is before the 04:00 reset, so it counts for the day before.
    started = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)  # 03:00 CEST
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=10))

    days = history_days(conn, NOW, AMS, FOUR)
    by_day = {d.day: d for d in days}
    assert len(by_day[date(2026, 9, 27)].episodes) == 1
    assert by_day[date(2026, 9, 28)].episodes == []


def test_session_after_reset_belongs_to_that_day(conn, episode_id, profile_id):
    started = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)  # 05:00 CEST
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=10))

    days = history_days(conn, NOW, AMS, FOUR)
    by_day = {d.day: d for d in days}
    assert len(by_day[date(2026, 9, 28)].episodes) == 1


def test_window_is_21_days_newest_first(conn, episode_id, profile_id):
    days = history_days(conn, NOW, AMS, FOUR)
    assert len(days) == 21
    assert days[0].day == date(2026, 9, 28)
    assert days[-1].day == date(2026, 9, 8)
    assert days == sorted(days, key=lambda d: d.day, reverse=True)


def test_session_outside_window_is_excluded(conn, episode_id, profile_id):
    too_old = NOW - timedelta(days=25)
    open_close(conn, episode_id, profile_id, too_old, too_old + timedelta(minutes=5))

    days = history_days(conn, NOW, AMS, FOUR)
    assert sum(len(d.episodes) for d in days) == 0


def test_session_at_the_edge_of_window_is_included(conn, episode_id, profile_id):
    # 20 days before "today" (local) is the oldest day in a 21-day window.
    started = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)  # 05:00 CEST, day 2026-09-08
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=5))

    days = history_days(conn, NOW, AMS, FOUR)
    by_day = {d.day: d for d in days}
    assert len(by_day[date(2026, 9, 8)].episodes) == 1


def test_deleted_episode_shows_placeholder(conn, episode_id, profile_id):
    started = NOW - timedelta(hours=1)
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=10))
    library.delete_episode(conn, Path("/nonexistent-media-dir"), episode_id)

    days = history_days(conn, NOW, AMS, FOUR)
    ep = next(e for d in days for e in d.episodes)
    assert ep.episode_id is None
    assert ep.title == "deleted episode"
    assert ep.show_name is None


def test_open_session_shows_watching_now(conn, episode_id, profile_id):
    started = NOW - timedelta(minutes=5)
    store.open_watch_session(conn, episode_id, [profile_id], started)

    days = history_days(conn, NOW, AMS, FOUR)
    ep = next(e for d in days for e in d.episodes)
    assert ep.ended_at is None
    assert ep.end_reason_label == WATCHING_NOW_LABEL


@pytest.mark.parametrize("reason,label", [
    (EndReason.FINISHED, "finished"),
    (EndReason.TIME_UP, "time was up"),
    (EndReason.PARENT_STOP, "stopped by a parent"),
    (EndReason.BLOCKED, "blocked"),
])
def test_end_reason_label(conn, episode_id, profile_id, reason, label):
    started = NOW - timedelta(minutes=10)
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=5), reason=reason)

    days = history_days(conn, NOW, AMS, FOUR)
    ep = next(e for d in days for e in d.episodes)
    assert ep.end_reason_label == label


def test_minutes_rounded_from_seconds_counted(conn, episode_id, profile_id):
    started = NOW - timedelta(minutes=10)
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=5), seconds=330)

    days = history_days(conn, NOW, AMS, FOUR)
    ep = next(e for d in days for e in d.episodes)
    assert ep.minutes == 6  # 330s -> 5.5 min, rounds to 6


def test_overrides_grouped_by_their_own_day(conn, profile_id):
    store.log_override(conn, profile_id, date(2026, 9, 27), "extra_minutes", 15, NOW)
    store.log_override(conn, profile_id, date(2026, 9, 28), "block", 1, NOW)

    days = history_days(conn, NOW, AMS, FOUR)
    by_day = {d.day: d for d in days}
    assert [o.kind for o in by_day[date(2026, 9, 27)].overrides] == ["extra_minutes"]
    assert [o.kind for o in by_day[date(2026, 9, 28)].overrides] == ["block"]


def test_override_outside_window_is_excluded(conn, profile_id):
    store.log_override(conn, profile_id, date(2026, 8, 1), "extra_minutes", 15, NOW)

    days = history_days(conn, NOW, AMS, FOUR)
    assert sum(len(d.overrides) for d in days) == 0


# --------------------------------------------------------------------------- profiles (step 8, PR-3)


@pytest.fixture
def second_profile(conn) -> int:
    conn.execute("UPDATE profile SET name = 'Mila', avatar = 'fox' WHERE id = 1")
    conn.execute("INSERT INTO profile (id, name, avatar, sort_order, created_at) VALUES (2, 'Noor', 'owl', 2, 'x')")
    return 2


def test_episode_lists_who_watched_in_admin_order(conn, episode_id, profile_id, second_profile):
    started = NOW - timedelta(minutes=10)
    session_id = store.open_watch_session(conn, episode_id, [second_profile, profile_id], started)
    store.close_watch_session(conn, session_id, EndReason.FINISHED, NOW, 300.0)

    ep = next(e for d in history_days(conn, NOW, AMS, FOUR) for e in d.episodes)
    assert [(p.id, p.name, p.avatar) for p in ep.profiles] == [(1, "Mila", "fox"), (2, "Noor", "owl")]


def test_profile_filter_keeps_only_sessions_that_profile_was_in(conn, episode_id, profile_id, second_profile):
    started = NOW - timedelta(minutes=30)
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=5))
    both = store.open_watch_session(conn, episode_id, [profile_id, second_profile], started + timedelta(minutes=10))
    store.close_watch_session(conn, both, EndReason.STOPPED, started + timedelta(minutes=15), 300.0)

    def count(pid):
        return sum(len(d.episodes) for d in history_days(conn, NOW, AMS, FOUR, profile_id=pid))

    assert (count(None), count(profile_id), count(second_profile)) == (2, 2, 1)
    # the filtered rows still list everyone who watched
    ep = next(e for d in history_days(conn, NOW, AMS, FOUR, profile_id=second_profile) for e in d.episodes)
    assert [p.id for p in ep.profiles] == [1, 2]


def test_profile_filter_keeps_that_profiles_overrides_and_everyones(conn, profile_id, second_profile):
    store.log_override(conn, profile_id, date(2026, 9, 28), "extra_minutes", 15, NOW)
    store.log_override(conn, second_profile, date(2026, 9, 28), "block", 1, NOW)
    store.log_override(conn, None, date(2026, 9, 28), "stop_now", None, NOW)

    def kinds(pid):
        return sorted(o.kind for d in history_days(conn, NOW, AMS, FOUR, profile_id=pid) for o in d.overrides)

    assert kinds(None) == ["block", "extra_minutes", "stop_now"]
    assert kinds(second_profile) == ["block", "stop_now"]


def test_override_carries_its_profile(conn, second_profile):
    store.log_override(conn, second_profile, date(2026, 9, 28), "block", 1, NOW)
    store.log_override(conn, None, date(2026, 9, 28), "stop_now", None, NOW)
    overrides = next(d for d in history_days(conn, NOW, AMS, FOUR) if d.overrides).overrides
    assert [(o.kind, o.profile.name if o.profile else None) for o in overrides] == [("block", "Noor"), ("stop_now", None)]


def test_override_carries_its_source(conn, profile_id):
    """HA-7: NULL source = the admin pages; otherwise the API token name."""
    store.log_override(conn, profile_id, date(2026, 9, 28), "block", 1, NOW)
    store.log_override(conn, profile_id, date(2026, 9, 28), "extra_minutes", 5, NOW)
    conn.execute("UPDATE override_log SET source = ? WHERE kind = ?", ("Home Assistant", "extra_minutes"))
    by_day = {d.day: d for d in history_days(conn, NOW, AMS, FOUR)}
    assert {o.kind: o.source for o in by_day[date(2026, 9, 28)].overrides} == {"block": None, "extra_minutes": "Home Assistant"}
