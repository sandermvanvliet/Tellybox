"""The receiver event log (CR-6): store functions, the summary behind the cast state, the purge, migration 011."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tellybox import db, library, purge, store
from tellybox.db import open_db

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "t.db")


def rec(conn, minutes_ago, kind, detail=None):
    store.record_receiver_event(conn, NOW - timedelta(minutes=minutes_ago), kind, detail)


def test_migration_011_creates_the_table_and_index(conn):
    assert db.migrations()[-1][0] >= 11
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(receiver_event)")}
    assert cols == {"id", "at", "kind", "detail", "duration_ms", "episode_id"}
    assert "receiver_event_at" in {r["name"] for r in conn.execute("PRAGMA index_list(receiver_event)")}


def test_record_and_read_back(conn):
    store.record_receiver_event(conn, NOW, "launch_ok", "attempt 1", duration_ms=3200)
    (row,) = conn.execute("SELECT * FROM receiver_event").fetchall()
    assert (row["kind"], row["detail"], row["duration_ms"], row["episode_id"]) == ("launch_ok", "attempt 1", 3200, None)


def test_deleting_an_episode_keeps_its_events(conn):
    show = library.create_show(conn, "S", now=NOW)
    ep = library.add_episode(conn, show, "E", "s/e.mp4", now=NOW, duration_s=60)
    store.record_receiver_event(conn, NOW, "lost", "x", episode_id=ep)
    conn.execute("DELETE FROM episode WHERE id = ?", (ep,))
    assert conn.execute("SELECT episode_id FROM receiver_event").fetchone()[0] is None


def test_a_failed_write_never_raises(conn):
    conn.execute("DROP TABLE receiver_event")
    store.record_receiver_event(conn, NOW, "launch_ok")  # no table: logged, not raised


def test_summary_of_an_empty_log(conn):
    assert store.receiver_summary(conn, NOW) == {"last_failure": None, "failures_24h": 0, "launches_24h": 0}


def test_summary_counts_the_last_24_hours_and_finds_the_last_failure(conn):
    rec(conn, 26 * 60, "launch_failed", "too old")
    rec(conn, 26 * 60, "launch_ok")
    rec(conn, 120, "launch_failed", "attempt 1: launch timed out")
    rec(conn, 119, "launch_ok", "attempt 2")
    rec(conn, 60, "refused", "attempt 1: launch failed: CANCELLED")
    rec(conn, 59, "fallback", "until 12:30")  # not a failure itself
    rec(conn, 10, "launch_ok")
    rec(conn, 5, "recovered")  # nor is this
    summary = store.receiver_summary(conn, NOW)
    assert summary["launches_24h"] == 2 and summary["failures_24h"] == 2
    assert summary["last_failure"] == {
        "kind": "refused", "detail": "attempt 1: launch failed: CANCELLED", "at": "2026-10-01T11:00:00.000+00:00"}


def test_the_last_failure_may_be_older_than_a_day(conn):
    store.record_receiver_event(conn, NOW - timedelta(days=3), "page_error", "boom")
    summary = store.receiver_summary(conn, NOW)
    assert summary["failures_24h"] == 0 and summary["last_failure"]["detail"] == "boom"


def test_purge_removes_events_older_than_21_days(conn):
    store.record_receiver_event(conn, NOW - timedelta(days=22), "launch_failed")
    store.record_receiver_event(conn, NOW - timedelta(days=20), "launch_ok")
    store.record_receiver_event(conn, NOW - timedelta(days=5), "lost")
    counts = purge.purge(conn, NOW, TZ, time(4, 0))
    assert counts["receiver_event"] == 1
    assert [r["kind"] for r in conn.execute("SELECT kind FROM receiver_event ORDER BY id")] == ["launch_ok", "lost"]
