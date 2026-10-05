"""The show allow-list (PR-5, PR-6, PR-8, A-37): helper API and the migration."""

import pytest

from tellybox import db, library, show_access
from tellybox.db import to_db
from tests.web.conftest import NOW

pytestmark = pytest.mark.strict_access


@pytest.fixture
def conn():
    c = db.open_db(":memory:")
    for n in (2, 3):
        c.execute("INSERT INTO profile (id, name, created_at) VALUES (?, ?, ?)", (n, f"P{n}", to_db(NOW)))
    yield c
    c.close()


def make_show(conn, name="S"):
    return library.create_show(conn, name, now=NOW)


def test_a_new_show_and_a_new_profile_start_hidden(conn):  # PR-5
    s = make_show(conn)
    assert show_access.visible_show_ids(conn, [1, 2, 3]) == set()
    assert show_access.visible_show_ids(conn, [1]) == set()
    assert not show_access.can_watch(conn, [1], s)
    conn.execute("INSERT INTO profile (id, name, created_at) VALUES (4, 'New', 'x')")
    assert show_access.visible_count_by_profile(conn) == {1: 0, 2: 0, 3: 0, 4: 0}


def test_grant_revoke_and_intersection(conn):  # PR-6
    a, b = make_show(conn, "A"), make_show(conn, "B")
    show_access.grant(conn, 1, a)
    show_access.grant(conn, 1, a)  # idempotent
    show_access.grant(conn, 1, b)
    show_access.grant(conn, 2, a)
    assert show_access.visible_show_ids(conn, [1]) == {a, b}
    assert show_access.visible_show_ids(conn, [1, 2]) == {a}
    assert show_access.visible_show_ids(conn, [1, 2, 3]) == set()
    assert show_access.visible_show_ids(conn, []) == set()
    assert show_access.can_watch(conn, [1, 2], a) and not show_access.can_watch(conn, [1, 2], b)
    assert not show_access.can_watch(conn, [], a)
    show_access.revoke(conn, 2, a)
    assert not show_access.can_watch(conn, [1, 2], a)
    show_access.revoke(conn, 2, a)  # no error


def test_bulk_setters_replace_exactly(conn):
    a, b, c = make_show(conn, "A"), make_show(conn, "B"), make_show(conn, "C")
    show_access.set_profile_shows(conn, 1, [a, b])
    show_access.set_profile_shows(conn, 1, [b, c])
    assert show_access.visible_show_ids(conn, [1]) == {b, c}
    show_access.set_show_profiles(conn, b, [2, 3])
    assert [p for p in (1, 2, 3) if show_access.can_watch(conn, [p], b)] == [2, 3]
    show_access.set_show_profiles(conn, b, [])
    assert show_access.visible_count_by_profile(conn) == {1: 1, 2: 0, 3: 0}


def test_copy_from_is_a_one_off(conn):  # PR-5
    a, b = make_show(conn, "A"), make_show(conn, "B")
    show_access.set_profile_shows(conn, 1, [a])
    show_access.copy_from(conn, 1, 2)
    assert show_access.visible_show_ids(conn, [2]) == {a}
    show_access.grant(conn, 1, b)  # later changes do not follow
    show_access.revoke(conn, 1, a)
    assert show_access.visible_show_ids(conn, [2]) == {a}


def test_grants_follow_deletes(conn):
    a = make_show(conn)
    show_access.set_show_profiles(conn, a, [1, 2])
    conn.execute("DELETE FROM profile WHERE id = 2")
    assert show_access.visible_count_by_profile(conn) == {1: 1, 3: 0}
    conn.execute("DELETE FROM show WHERE id = ?", (a,))
    assert conn.execute("SELECT COUNT(*) FROM profile_show").fetchone()[0] == 0


def test_migration_grants_every_existing_pair(tmp_path):  # A-37: upgrading changes nothing visible
    conn = db.connect(tmp_path / "t.db")
    old = [m for m in db.migrations() if m[0] < 20]
    for version, _name, sql in old:
        for statement in db._split_sql(sql):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("INSERT INTO profile (id, name, created_at) VALUES (2, 'B', 'x')")
    s1 = conn.execute("INSERT INTO show (name, created_at) VALUES ('One', 'x')").lastrowid
    s2 = conn.execute("INSERT INTO show (name, created_at) VALUES ('Two', 'x')").lastrowid
    assert db.migrate(conn) >= 20
    assert {(r[0], r[1]) for r in conn.execute("SELECT profile_id, show_id FROM profile_show")} == {
        (p, s) for p in (1, 2) for s in (s1, s2)}
    # ...and what comes afterwards starts hidden.
    s3 = make_show(conn)
    assert not show_access.can_watch(conn, [1], s3)
