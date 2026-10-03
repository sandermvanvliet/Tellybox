"""Purge of closed history and old jobs (AD-5). Never touches an open session or today's rows."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tellybox import jobs, purge
from tellybox.db import open_db, to_db
from tellybox.jobs import JobType
from tellybox import auth

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)  # 14:00 CEST; today's watch day is 2026-09-28
RESET = time(4, 0)


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "t.db")


def _watch_session(conn, session_id, *, started, ended):
    conn.execute(
        "INSERT INTO watch_session (id, episode_id, started_at, ended_at, seconds_counted) VALUES (?, NULL, ?, ?, 0)",
        (session_id, to_db(started), to_db(ended)),
    )


def _daily_usage(conn, day, profile_id=1):
    conn.execute(
        "INSERT INTO daily_usage (profile_id, day, seconds_used, updated_at) VALUES (?, ?, 0, ?)",
        (profile_id, day, to_db(NOW)),
    )


def _override(conn, day, profile_id=1):
    conn.execute(
        "INSERT INTO override_log (profile_id, day, kind, created_at) VALUES (?, ?, 'unlimited', ?)",
        (profile_id, day, to_db(NOW)),
    )


def test_removes_only_closed_sessions_ended_long_ago(conn):
    _watch_session(conn, 1, started=NOW - timedelta(days=40), ended=NOW - timedelta(days=22))  # old, closed
    _watch_session(conn, 2, started=NOW - timedelta(days=10), ended=NOW - timedelta(days=5))  # recent, closed
    _watch_session(conn, 3, started=NOW - timedelta(days=40), ended=NOW - timedelta(days=21))  # exactly 21d: kept
    _watch_session(conn, 4, started=NOW - timedelta(days=40), ended=None)  # open: never touched

    counts = purge.purge(conn, NOW, TZ, RESET)

    remaining = {r["id"] for r in conn.execute("SELECT id FROM watch_session")}
    assert remaining == {2, 3, 4}
    assert counts["watch_session"] == 1


def test_watch_session_profile_rows_cascade(conn):
    _watch_session(conn, 1, started=NOW - timedelta(days=40), ended=NOW - timedelta(days=22))
    conn.execute("INSERT INTO watch_session_profile (watch_session_id, profile_id) VALUES (1, 1)")

    purge.purge(conn, NOW, TZ, RESET)

    assert conn.execute("SELECT count(*) FROM watch_session_profile").fetchone()[0] == 0


def test_daily_usage_and_override_log_keep_days_within_retention(conn):
    _daily_usage(conn, "2026-08-01")  # long past: removed
    _daily_usage(conn, "2026-09-07")  # exactly 21 days before today: kept
    _daily_usage(conn, "2026-09-28")  # today: kept
    _override(conn, "2026-08-01")
    _override(conn, "2026-09-07")

    counts = purge.purge(conn, NOW, TZ, RESET)

    assert counts["daily_usage"] == 1
    assert counts["override_log"] == 1
    remaining_days = {r["day"] for r in conn.execute("SELECT day FROM daily_usage")}
    assert remaining_days == {"2026-09-07", "2026-09-28"}


def test_removes_old_finished_jobs_but_keeps_recent_and_pending(conn):
    old = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=NOW - timedelta(days=40))
    jobs.complete(conn, old, now=NOW - timedelta(days=31))
    recent = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=NOW - timedelta(days=5))
    jobs.complete(conn, recent, now=NOW - timedelta(days=5))
    pending = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=NOW)

    counts = purge.purge(conn, NOW, TZ, RESET)

    remaining = {r["id"] for r in conn.execute("SELECT id FROM job")}
    assert remaining == {recent, pending}
    assert counts["job"] == 1


def test_removes_expired_admin_sessions_and_stale_throttle_rows(conn):
    auth.create_session(conn, NOW - timedelta(days=31))  # expired (30-day sliding window)
    auth.create_session(conn, NOW - timedelta(days=1))  # still live
    conn.execute(
        "INSERT INTO login_throttle (address, failures, next_allowed_at, updated_at) VALUES (?, 1, ?, ?)",
        ("1.2.3.4", to_db(NOW - timedelta(days=2)), to_db(NOW - timedelta(days=2))),
    )
    conn.execute(
        "INSERT INTO login_throttle (address, failures, next_allowed_at, updated_at) VALUES (?, 1, ?, ?)",
        ("5.6.7.8", to_db(NOW), to_db(NOW)),
    )

    counts = purge.purge(conn, NOW, TZ, RESET)

    assert counts["admin_session"] == 1
    assert conn.execute("SELECT count(*) FROM admin_session").fetchone()[0] == 1
    assert counts["login_throttle"] == 1
    assert conn.execute("SELECT count(*) FROM login_throttle").fetchone()[0] == 1


def test_removes_oidc_sign_ins_older_than_10_minutes(conn):  # AD-6
    for state, age in (("stale", timedelta(minutes=11)), ("edge", timedelta(minutes=10)),
                       ("fresh", timedelta(minutes=2))):
        conn.execute(
            """INSERT INTO oidc_login (state_hash, nonce, code_verifier, next, browser_hash, created_at)
               VALUES (?, 'n', 'v', '/admin', 'b', ?)""",
            (state, to_db(NOW - age)),
        )

    counts = purge.purge(conn, NOW, TZ, RESET)

    assert counts["oidc_login"] == 2
    assert [r[0] for r in conn.execute("SELECT state_hash FROM oidc_login")] == ["fresh"]


def test_purge_is_idempotent(conn):
    _watch_session(conn, 1, started=NOW - timedelta(days=40), ended=NOW - timedelta(days=22))
    purge.purge(conn, NOW, TZ, RESET)
    counts = purge.purge(conn, NOW, TZ, RESET)
    assert all(v == 0 for v in counts.values())
