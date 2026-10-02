"""Per-profile limits resolution (A-23): inherit, custom, unlimited modes.

Tests for store.effective_allowance_s, effective_max_session_s, and profile_policies
resolving modes correctly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, UTC

import pytest

from tellybox import db, store
from tellybox.timer import CountingMode

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    """In-memory DB with default settings and profiles."""
    c = db.open_db(":memory:")
    c.row_factory = sqlite3.Row
    # Settings table is created by migration 001
    c.execute("SELECT 1 FROM settings WHERE id = 1")  # migrations are applied by open_db
    yield c
    c.close()


def test_effective_allowance_custom(conn):
    """A custom allowance uses the profile's daily_allowance_min."""
    conn.execute("UPDATE profile SET allowance_mode = 'custom', daily_allowance_min = 45 WHERE id = 1")
    assert store.effective_allowance_s(conn, 1) == 45 * 60.0


def test_effective_allowance_inherit(conn):
    """An inherit allowance uses the settings.default_allowance_min."""
    conn.execute("UPDATE settings SET default_allowance_min = 90 WHERE id = 1")
    conn.execute("UPDATE profile SET allowance_mode = 'inherit', daily_allowance_min = 45 WHERE id = 1")
    assert store.effective_allowance_s(conn, 1) == 90 * 60.0


def test_effective_allowance_unlimited(conn):
    """An unlimited allowance resolves to None."""
    conn.execute("UPDATE profile SET allowance_mode = 'unlimited', daily_allowance_min = 45 WHERE id = 1")
    assert store.effective_allowance_s(conn, 1) is None


def test_effective_allowance_inherit_uses_default_60_when_not_set(conn):
    """When settings.default_allowance_min is not set, default to 60 minutes (A-23)."""
    # SQLite doesn't have a way to unset a value, but test the logic by checking the default
    c = db.open_db(":memory:")
    c.row_factory = sqlite3.Row
    # Get the default from settings
    c.execute("UPDATE profile SET allowance_mode = 'inherit' WHERE id = 1")
    assert store.effective_allowance_s(c, 1) == 60 * 60.0


def test_effective_max_session_custom(conn):
    """A custom max session uses the profile's max_session_min."""
    conn.execute("UPDATE profile SET max_session_mode = 'custom', max_session_min = 120 WHERE id = 1")
    assert store.effective_max_session_s(conn, 1) == 120 * 60.0


def test_effective_max_session_inherit(conn):
    """An inherit max session uses the settings.default_max_session_min."""
    conn.execute("UPDATE settings SET default_max_session_min = 180 WHERE id = 1")
    conn.execute("UPDATE profile SET max_session_mode = 'inherit', max_session_min = 120 WHERE id = 1")
    assert store.effective_max_session_s(conn, 1) == 180 * 60.0


def test_effective_max_session_unlimited(conn):
    """An unlimited max session resolves to None."""
    conn.execute("UPDATE profile SET max_session_mode = 'unlimited', max_session_min = 120 WHERE id = 1")
    assert store.effective_max_session_s(conn, 1) is None


def test_effective_max_session_inherit_uses_default_90_when_not_set(conn):
    """When settings.default_max_session_min is not set, default to 90 minutes (A-23)."""
    c = db.open_db(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("UPDATE profile SET max_session_mode = 'inherit' WHERE id = 1")
    assert store.effective_max_session_s(c, 1) == 90 * 60.0


def test_profile_policies_resolve_inherit(conn):
    """profile_policies resolves inherit allowances to settings default."""
    conn.execute("UPDATE settings SET default_allowance_min = 120, default_max_session_min = 240 WHERE id = 1")
    conn.execute("UPDATE profile SET allowance_mode = 'inherit', max_session_mode = 'inherit', daily_allowance_min = 45, max_session_min = 60 WHERE id = 1")
    policies = store.profile_policies(conn, ids=[1])
    assert len(policies) == 1
    assert policies[0].profile_id == 1
    assert policies[0].allowance_s == 120 * 60.0
    assert policies[0].max_session_s == 240 * 60.0


def test_profile_policies_resolve_custom(conn):
    """profile_policies resolves custom allowances to profile values."""
    conn.execute("UPDATE profile SET allowance_mode = 'custom', max_session_mode = 'custom', daily_allowance_min = 45, max_session_min = 60 WHERE id = 1")
    policies = store.profile_policies(conn, ids=[1])
    assert len(policies) == 1
    assert policies[0].profile_id == 1
    assert policies[0].allowance_s == 45 * 60.0
    assert policies[0].max_session_s == 60 * 60.0


def test_profile_policies_resolve_unlimited(conn):
    """profile_policies resolves unlimited allowances to None."""
    conn.execute("UPDATE profile SET allowance_mode = 'unlimited', max_session_mode = 'unlimited' WHERE id = 1")
    policies = store.profile_policies(conn, ids=[1])
    assert len(policies) == 1
    assert policies[0].profile_id == 1
    assert policies[0].allowance_s is None
    assert policies[0].max_session_s is None


def test_profile_policies_mixed_modes(conn):
    """profile_policies handles mixed modes across profiles."""
    conn.execute("UPDATE settings SET default_allowance_min = 100, default_max_session_min = 200 WHERE id = 1")
    conn.execute("UPDATE profile SET allowance_mode = 'custom', max_session_mode = 'custom', daily_allowance_min = 50, max_session_min = 100 WHERE id = 1")
    conn.execute("INSERT INTO profile (id, name, allowance_mode, daily_allowance_min, max_session_mode, max_session_min, counting_mode, created_at) VALUES (2, 'inherit', 'inherit', 999, 'inherit', 999, 'ignore_pauses', ?)", (NOW.isoformat(),))
    conn.execute("INSERT INTO profile (id, name, allowance_mode, daily_allowance_min, max_session_mode, max_session_min, counting_mode, created_at) VALUES (3, 'unlimited', 'unlimited', 50, 'unlimited', 100, 'ignore_pauses', ?)", (NOW.isoformat(),))

    policies = store.profile_policies(conn)
    assert len(policies) == 3

    # Profile 1: custom
    assert policies[0].profile_id == 1
    assert policies[0].allowance_s == 50 * 60.0
    assert policies[0].max_session_s == 100 * 60.0

    # Profile 2: inherit
    assert policies[1].profile_id == 2
    assert policies[1].allowance_s == 100 * 60.0
    assert policies[1].max_session_s == 200 * 60.0

    # Profile 3: unlimited
    assert policies[2].profile_id == 3
    assert policies[2].allowance_s is None
    assert policies[2].max_session_s is None


def test_profile_policies_preserves_mode(conn):
    """profile_policies preserves the counting_mode."""
    conn.execute("UPDATE profile SET counting_mode = 'wall_clock' WHERE id = 1")
    policies = store.profile_policies(conn, ids=[1])
    assert policies[0].mode == CountingMode.WALL_CLOCK


def test_effective_allowance_nonexistent_profile(conn):
    """effective_allowance_s returns None for a nonexistent profile."""
    assert store.effective_allowance_s(conn, 9999) is None


def test_effective_max_session_nonexistent_profile(conn):
    """effective_max_session_s returns None for a nonexistent profile."""
    assert store.effective_max_session_s(conn, 9999) is None


def test_profile_policies_ids_filter(conn):
    """profile_policies respects the ids parameter."""
    conn.execute("INSERT INTO profile (id, name, created_at) VALUES (2, 'B', ?)", (NOW.isoformat(),))
    policies = store.profile_policies(conn, ids=[2])
    assert len(policies) == 1
    assert policies[0].profile_id == 2


def test_profile_policies_empty_ids(conn):
    """profile_policies with empty ids list returns nothing."""
    policies = store.profile_policies(conn, ids=[])
    assert len(policies) == 0
