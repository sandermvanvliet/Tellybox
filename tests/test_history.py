"""History queries for the admin History page (AD-4, AD-5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tellybox import db, library, store
from tellybox.cast.controller import EndReason
from tellybox.history import HISTORY_DAYS, WATCHING_NOW_LABEL, history_days, usage_history

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


def test_history_rows_carry_the_target(conn, episode_id, profile_id):  # PB-8, AD-4
    started = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=10))
    dev = store.open_watch_session(
        conn, episode_id, [profile_id], started + timedelta(minutes=20), target="device", device_label="iPhone Safari"
    )
    store.close_watch_session(conn, dev, EndReason.STOPPED, started + timedelta(minutes=30), 600)

    tv_row, dev_row = next(d for d in history_days(conn, NOW, AMS, FOUR) if d.episodes).episodes
    assert (tv_row.target, tv_row.device_label) == ("tv", None)
    assert (dev_row.target, dev_row.device_label) == ("device", "iPhone Safari")
    assert [s.target for s in store.open_watch_sessions(conn)] == []


def test_open_watch_sessions_filter_by_target(conn, episode_id, profile_id):  # NF-7, PB-8
    store.open_watch_session(conn, episode_id, [profile_id], NOW)
    store.open_watch_session(conn, episode_id, [profile_id], NOW, target="device", device_label="Pixel Chrome")
    assert [s.target for s in store.open_watch_sessions(conn)] == ["tv", "device"]
    assert [s.target for s in store.open_watch_sessions(conn, target="tv")] == ["tv"]
    (device,) = store.open_watch_sessions(conn, target="device")
    assert device.device_label == "Pixel Chrome"


# --- usage_history (HA-12, A-38) ---------------------------------------------------------------


def put_usage(conn, profile, day, seconds=0.0, extra_min=0, unlimited=0, blocked=0):
    conn.execute(
        "INSERT INTO daily_usage (profile_id, day, seconds_used, extra_min, unlimited, blocked) VALUES (?, ?, ?, ?, ?, ?)",
        (profile, day, seconds, extra_min, unlimited, blocked),
    )


def test_usage_history_missing_days_are_zeros_newest_first(conn, profile_id):  # HA-12
    h = usage_history(conn, NOW, AMS, FOUR)
    assert h.today == date(2026, 9, 28)
    assert h.days == 7
    (p,) = h.profiles
    assert [d.date for d in p.days] == [date(2026, 9, 28) - timedelta(days=i) for i in range(7)]
    assert all((d.used_s, d.extra_s, d.unlimited, d.blocked) == (0, 0, False, False) for d in p.days)
    assert p.last_watched is None


def test_usage_history_reads_daily_usage_rounded(conn, profile_id):  # HA-12
    put_usage(conn, profile_id, "2026-09-28", 1199.6, 0)
    put_usage(conn, profile_id, "2026-09-27", 2710.2, 15)
    p = usage_history(conn, NOW, AMS, FOUR).profiles[0]
    assert (p.days[0].used_s, p.days[0].extra_s) == (1200, 0)
    assert (p.days[1].used_s, p.days[1].extra_s) == (2710, 900)
    assert isinstance(p.days[0].used_s, int)


def test_usage_history_flags(conn, profile_id):  # HA-12
    put_usage(conn, profile_id, "2026-09-28", 0, 0, unlimited=1)
    put_usage(conn, profile_id, "2026-09-27", 0, 0, blocked=1)
    p = usage_history(conn, NOW, AMS, FOUR).profiles[0]
    assert (p.days[0].unlimited, p.days[0].blocked) == (True, False)
    assert (p.days[1].unlimited, p.days[1].blocked) == (False, True)


def test_usage_history_day_boundary_follows_reset_time(conn, episode_id, profile_id):  # HA-12, WT-1
    now = datetime(2026, 9, 28, 2, 30, tzinfo=UTC)  # 04:30 CEST, just after the reset
    assert usage_history(conn, now, AMS, FOUR).today == date(2026, 9, 28)
    before = datetime(2026, 9, 28, 1, 59, tzinfo=UTC)  # 03:59 CEST
    assert usage_history(conn, before, AMS, FOUR).today == date(2026, 9, 27)
    put_usage(conn, profile_id, "2026-09-27", 100)
    put_usage(conn, profile_id, "2026-09-28", 200)
    # A session at 03:59 and one at 04:01 local are the latest-by-start; usage rows sit on each side.
    open_close(conn, episode_id, profile_id, before, before + timedelta(minutes=1))
    after = datetime(2026, 9, 28, 2, 1, tzinfo=UTC)  # 04:01 CEST
    open_close(conn, episode_id, profile_id, after, after + timedelta(minutes=1))
    at_before = usage_history(conn, before, AMS, FOUR, days=2).profiles[0]
    assert [(d.date, d.used_s) for d in at_before.days] == [(date(2026, 9, 27), 100), (date(2026, 9, 26), 0)]
    at_after = usage_history(conn, now, AMS, FOUR, days=2).profiles[0]
    assert [(d.date, d.used_s) for d in at_after.days] == [(date(2026, 9, 28), 200), (date(2026, 9, 27), 100)]
    assert at_after.last_watched.started_at == after


def test_usage_history_rows_outside_window_are_absent(conn, profile_id):  # HA-12, AD-5
    put_usage(conn, profile_id, "2026-09-20", 999)  # 8 days before today
    put_usage(conn, profile_id, "2026-09-29", 888)  # a future day is not an entry either
    p = usage_history(conn, NOW, AMS, FOUR, days=7).profiles[0]
    assert len(p.days) == 7
    assert all(d.used_s == 0 for d in p.days)
    p = usage_history(conn, NOW, AMS, FOUR, days=HISTORY_DAYS).profiles[0]
    assert len(p.days) == 21
    assert {d.date: d.used_s for d in p.days}[date(2026, 9, 20)] == 999
    put_usage(conn, profile_id, "2026-09-07", 777)  # 21 days back: one past the window
    p = usage_history(conn, NOW, AMS, FOUR, days=HISTORY_DAYS).profiles[0]
    assert date(2026, 9, 7) not in {d.date for d in p.days}


@pytest.mark.parametrize("days", [0, -1, HISTORY_DAYS + 1])
def test_usage_history_days_out_of_range(conn, profile_id, days):  # HA-12
    with pytest.raises(ValueError):
        usage_history(conn, NOW, AMS, FOUR, days=days)


@pytest.mark.parametrize("days", [1, HISTORY_DAYS])
def test_usage_history_days_bounds_accepted(conn, profile_id, days):  # HA-12
    h = usage_history(conn, NOW, AMS, FOUR, days=days)
    assert h.days == days
    assert len(h.profiles[0].days) == days


def test_usage_history_shared_session_counts_once_in_totals(conn, episode_id, profile_id, second_profile):  # HA-12
    started = NOW - timedelta(minutes=10)
    session_id = store.open_watch_session(conn, episode_id, [profile_id, second_profile], started)
    store.close_watch_session(conn, session_id, EndReason.FINISHED, NOW, 600.0)
    put_usage(conn, profile_id, "2026-09-28", 600)
    # second_profile has no daily_usage row: totals come from daily_usage only
    a, b = usage_history(conn, NOW, AMS, FOUR).profiles
    assert (a.days[0].used_s, b.days[0].used_s) == (600, 0)
    for p in (a, b):
        assert p.last_watched.title == "Hospital"
        assert p.last_watched.show == "Bluey"
        assert p.last_watched.episode_id == episode_id


def test_usage_history_last_watched_is_latest_by_start_with_ends_and_target(conn, episode_id, profile_id):  # A-38
    first = NOW - timedelta(hours=3)
    open_close(conn, episode_id, profile_id, first, first + timedelta(minutes=10))
    second = NOW - timedelta(hours=1)
    sid = store.open_watch_session(conn, episode_id, [profile_id], second, target="device", device_label="Pixel Chrome")
    store.close_watch_session(conn, sid, EndReason.STOPPED, second + timedelta(minutes=5), 300)
    lw = usage_history(conn, NOW, AMS, FOUR).profiles[0].last_watched
    assert lw.started_at == second
    assert lw.ended_at == second + timedelta(minutes=5)
    assert lw.started_at.tzinfo is not None and lw.target == "device"


def test_usage_history_open_session_has_no_end(conn, episode_id, profile_id):  # A-38
    store.open_watch_session(conn, episode_id, [profile_id], NOW - timedelta(minutes=5))
    lw = usage_history(conn, NOW, AMS, FOUR).profiles[0].last_watched
    assert lw.ended_at is None
    assert lw.target == "tv"


def test_usage_history_deleted_episode_has_no_title(conn, episode_id, profile_id):  # A-38
    started = NOW - timedelta(hours=1)
    open_close(conn, episode_id, profile_id, started, started + timedelta(minutes=10))
    library.delete_episode(conn, Path("/nonexistent-media-dir"), episode_id)
    lw = usage_history(conn, NOW, AMS, FOUR).profiles[0].last_watched
    assert (lw.episode_id, lw.title, lw.show) == (None, None, None)
    assert lw.started_at == started


def test_usage_history_profiles_in_admin_order(conn, profile_id, second_profile):  # HA-12
    conn.execute("UPDATE profile SET sort_order = 5 WHERE id = ?", (profile_id,))
    conn.execute("UPDATE profile SET sort_order = 1 WHERE id = ?", (second_profile,))
    h = usage_history(conn, NOW, AMS, FOUR)
    assert [(p.id, p.name) for p in h.profiles] == [(second_profile, "Noor"), (profile_id, "Mila")]


def test_usage_history_profile_ids_filter_keeps_admin_order(conn, profile_id, second_profile):  # HA-12
    only = usage_history(conn, NOW, AMS, FOUR, profile_ids=[second_profile])
    assert [p.id for p in only.profiles] == [second_profile]
    both = usage_history(conn, NOW, AMS, FOUR, profile_ids=[second_profile, profile_id])
    assert [p.id for p in both.profiles] == [profile_id, second_profile]


def test_usage_history_unknown_profile_id_raises(conn, profile_id):  # HA-12
    with pytest.raises(ValueError):
        usage_history(conn, NOW, AMS, FOUR, profile_ids=[profile_id, 999])
