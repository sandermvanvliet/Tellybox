"""Which shows each profile may see (PR-5..PR-8, A-37).

An allow-list of (profile, show) pairs; episodes inherit from their show. A group watching
together sees the intersection of its members' shows (PR-6). Everything that lists, picks,
autoplays or serves media goes through here, so visibility is enforced on the server on every
path (PR-7). Grants never touch positions or history, so revoking and re-granting restores resume.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def _ids(profile_ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(profile_ids))


def _marks(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def visible_show_ids(conn: sqlite3.Connection, profile_ids: Iterable[int]) -> set[int]:
    """Shows every profile of the group may see (PR-6). An empty group sees nothing."""
    ids = _ids(profile_ids)
    if not ids:
        return set()
    rows = conn.execute(
        f"""SELECT show_id FROM profile_show WHERE profile_id IN ({_marks(ids)})
            GROUP BY show_id HAVING COUNT(*) = ?""",
        (*ids, len(ids)),
    ).fetchall()
    return {r[0] for r in rows}


def can_watch(conn: sqlite3.Connection, profile_ids: Iterable[int], show_id: int) -> bool:
    """Whether the whole group may see this show."""
    ids = _ids(profile_ids)
    if not ids:
        return False
    n = conn.execute(
        f"SELECT COUNT(*) FROM profile_show WHERE show_id = ? AND profile_id IN ({_marks(ids)})",
        (show_id, *ids),
    ).fetchone()[0]
    return n == len(ids)


def grant(conn: sqlite3.Connection, profile_id: int, show_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO profile_show (profile_id, show_id) VALUES (?, ?)", (profile_id, show_id))


def revoke(conn: sqlite3.Connection, profile_id: int, show_id: int) -> None:
    conn.execute("DELETE FROM profile_show WHERE profile_id = ? AND show_id = ?", (profile_id, show_id))


def _replace(conn: sqlite3.Connection, column: str, key: int, others: Iterable[int]) -> None:
    other = "show_id" if column == "profile_id" else "profile_id"
    wanted = set(others)
    conn.execute("SAVEPOINT show_access")
    try:
        have = {r[0] for r in conn.execute(f"SELECT {other} FROM profile_show WHERE {column} = ?", (key,))}
        for o in have - wanted:
            conn.execute(f"DELETE FROM profile_show WHERE {column} = ? AND {other} = ?", (key, o))
        for o in wanted - have:
            conn.execute(f"INSERT INTO profile_show ({column}, {other}) VALUES (?, ?)", (key, o))
    except BaseException:
        conn.execute("ROLLBACK TO show_access")
        raise
    finally:
        conn.execute("RELEASE show_access")


def set_profile_shows(conn: sqlite3.Connection, profile_id: int, show_ids: Iterable[int]) -> None:
    """Make exactly these shows visible to the profile (the matrix row, AD-8)."""
    _replace(conn, "profile_id", profile_id, show_ids)


def set_show_profiles(conn: sqlite3.Connection, show_id: int, profile_ids: Iterable[int]) -> None:
    """Make the show visible to exactly these profiles (the "Visible to" checklist, AD-8)."""
    _replace(conn, "show_id", show_id, profile_ids)


def copy_from(conn: sqlite3.Connection, source_profile_id: int, target_profile_id: int) -> None:
    """One-off copy of the source's shows onto the target (PR-5); later changes are not shared."""
    conn.execute(
        "INSERT OR IGNORE INTO profile_show (profile_id, show_id) SELECT ?, show_id FROM profile_show WHERE profile_id = ?",
        (target_profile_id, source_profile_id),
    )


def grant_all(conn: sqlite3.Connection) -> None:
    """Every profile gets every show (the upgrade default and a test helper)."""
    conn.execute("INSERT OR IGNORE INTO profile_show (profile_id, show_id) SELECT p.id, s.id FROM profile p CROSS JOIN show s")


def visible_count_by_profile(conn: sqlite3.Connection) -> dict[int, int]:
    """profile id -> number of shows it may see (HA-10, AD-9); 0 for a profile with none."""
    rows = conn.execute(
        """SELECT p.id, COUNT(ps.show_id) FROM profile p LEFT JOIN profile_show ps ON ps.profile_id = p.id
           GROUP BY p.id"""
    ).fetchall()
    return {r[0]: r[1] for r in rows}
