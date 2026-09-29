"""Shows and episodes: queries for the cast controller, the dev CLI, ingest and
the admin library management (CI-4, CI-6, LM-1, LM-3).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tellybox.db import to_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Show:
    id: int
    name: str
    autoplay: bool  # LM-4, PB-3
    hidden: bool  # LM-3
    sort_order: int
    youtube_channel_id: str | None = None  # CI-4: default show per channel
    artwork_path: str | None = None  # relative to the media dir


@dataclass(frozen=True)
class Episode:
    id: int
    show_id: int
    title: str
    file_path: str  # relative to the media dir
    duration_s: float | None
    sort_order: int
    hidden: bool
    source_video_id: int | None = None
    start_s: float | None = None  # segment of the source (splitting, step 6)
    end_s: float | None = None
    thumbnail_path: str | None = None  # relative to the media dir


_EPISODE_COLS = (
    "id, show_id, title, file_path, duration_s, sort_order, hidden, source_video_id, start_s, end_s, thumbnail_path"
)
_SHOW_COLS = "id, name, autoplay, hidden, sort_order, youtube_channel_id, artwork_path"


def _show(row: sqlite3.Row) -> Show:
    return Show(
        id=row["id"],
        name=row["name"],
        autoplay=bool(row["autoplay"]),
        hidden=bool(row["hidden"]),
        sort_order=row["sort_order"],
        youtube_channel_id=row["youtube_channel_id"],
        artwork_path=row["artwork_path"],
    )


def _episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        show_id=row["show_id"],
        title=row["title"],
        file_path=row["file_path"],
        duration_s=row["duration_s"],
        sort_order=row["sort_order"],
        hidden=bool(row["hidden"]),
        source_video_id=row["source_video_id"],
        start_s=row["start_s"],
        end_s=row["end_s"],
        thumbnail_path=row["thumbnail_path"],
    )


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE ... COMMIT, or join the caller's transaction if one is open."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def create_show(
    conn: sqlite3.Connection,
    name: str,
    *,
    now: datetime,
    autoplay: bool = True,
    youtube_channel_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO show (name, autoplay, youtube_channel_id, created_at) VALUES (?, ?, ?, ?)",
        (name, int(autoplay), youtube_channel_id, to_db(now)),
    )
    return cur.lastrowid


def add_episode(
    conn: sqlite3.Connection,
    show_id: int,
    title: str,
    file_path: str,
    *,
    now: datetime,
    duration_s: float | None = None,
    sort_order: int | None = None,
    source_video_id: int | None = None,
    thumbnail_path: str | None = None,
    hidden: bool = False,
) -> int:
    values = (show_id, title, file_path, duration_s, source_video_id, thumbnail_path, int(hidden), to_db(now))
    cols = "show_id, title, file_path, duration_s, source_video_id, thumbnail_path, hidden, created_at, sort_order"
    if sort_order is None:
        # Single statement, so concurrent writers can't pick the same slot.
        cur = conn.execute(
            f"""INSERT INTO episode ({cols})
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(MAX(sort_order) + 1, 0) FROM episode WHERE show_id = ?""",
            (*values, show_id),
        )
    else:
        cur = conn.execute(
            f"INSERT INTO episode ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, sort_order)
        )
    return cur.lastrowid


def get_show(conn: sqlite3.Connection, show_id: int) -> Show | None:
    row = conn.execute(f"SELECT {_SHOW_COLS} FROM show WHERE id = ?", (show_id,)).fetchone()
    return _show(row) if row else None


def list_shows(conn: sqlite3.Connection) -> list[Show]:
    """Every show (hidden included) in admin order; used e.g. for the "move to another show" picker."""
    return [_show(r) for r in conn.execute(f"SELECT {_SHOW_COLS} FROM show ORDER BY sort_order, id")]


def find_show_by_channel(conn: sqlite3.Connection, channel_id: str) -> Show | None:
    """The show new videos of this YouTube channel go to by default (CI-4); oldest match."""
    row = conn.execute(
        f"SELECT {_SHOW_COLS} FROM show WHERE youtube_channel_id = ? ORDER BY id LIMIT 1", (channel_id,)
    ).fetchone()
    return _show(row) if row else None


def get_episode(conn: sqlite3.Connection, episode_id: int) -> Episode | None:
    row = conn.execute(f"SELECT {_EPISODE_COLS} FROM episode WHERE id = ?", (episode_id,)).fetchone()
    return _episode(row) if row else None


def list_episodes(conn: sqlite3.Connection, show_id: int, *, include_hidden: bool = False) -> list[Episode]:
    where = "show_id = ?" if include_hidden else "show_id = ? AND hidden = 0"
    rows = conn.execute(
        f"SELECT {_EPISODE_COLS} FROM episode WHERE {where} ORDER BY sort_order, id", (show_id,)
    ).fetchall()
    return [_episode(r) for r in rows]


def next_episode(conn: sqlite3.Connection, episode_id: int) -> Episode | None:
    """Next visible episode of the same show by (sort_order, id); None at the end (PB-3)."""
    row = conn.execute(
        f"""SELECT {', '.join('n.' + c for c in _EPISODE_COLS.split(', '))}
            FROM episode cur
            JOIN episode n ON n.show_id = cur.show_id
            WHERE cur.id = ?
              AND n.hidden = 0
              AND (n.sort_order > cur.sort_order OR (n.sort_order = cur.sort_order AND n.id > cur.id))
            ORDER BY n.sort_order, n.id
            LIMIT 1""",
        (episode_id,),
    ).fetchone()
    return _episode(row) if row else None


def _update_one(conn: sqlite3.Connection, sql: str, params: tuple, key: int) -> None:
    if conn.execute(sql, params).rowcount == 0:
        raise KeyError(key)


def rename_show(conn: sqlite3.Connection, show_id: int, name: str) -> None:  # CI-4, LM-1
    name = name.strip()
    if not name:
        raise ValueError("show name must not be empty")
    _update_one(conn, "UPDATE show SET name = ? WHERE id = ?", (name, show_id), show_id)


def merge_shows(conn: sqlite3.Connection, into_id: int, from_id: int, *, now: datetime) -> None:
    """Move every episode and source video of `from` to the end of `into`, then drop `from` (CI-4).

    `from`'s artwork file is left on disk (merge has no media dir); disk_usage no longer counts it.
    """
    if into_id == from_id:
        raise ValueError("cannot merge a show into itself")
    with _transaction(conn):
        for sid in (into_id, from_id):
            if conn.execute("SELECT 1 FROM show WHERE id = ?", (sid,)).fetchone() is None:
                raise KeyError(sid)
        base = conn.execute(
            "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM episode WHERE show_id = ?", (into_id,)
        ).fetchone()[0]
        moving = conn.execute(
            "SELECT id FROM episode WHERE show_id = ? ORDER BY sort_order, id", (from_id,)
        ).fetchall()
        conn.executemany(
            "UPDATE episode SET show_id = ?, sort_order = ? WHERE id = ?",
            [(into_id, base + i, row["id"]) for i, row in enumerate(moving)],
        )
        conn.execute(
            "UPDATE source_video SET show_id = ?, updated_at = ? WHERE show_id = ?", (into_id, to_db(now), from_id)
        )
        conn.execute("DELETE FROM show WHERE id = ?", (from_id,))


def set_episode_hidden(conn: sqlite3.Connection, episode_id: int, hidden: bool) -> None:  # LM-3
    _update_one(conn, "UPDATE episode SET hidden = ? WHERE id = ?", (int(hidden), episode_id), episode_id)


def set_show_hidden(conn: sqlite3.Connection, show_id: int, hidden: bool) -> None:  # LM-3
    _update_one(conn, "UPDATE show SET hidden = ? WHERE id = ?", (int(hidden), show_id), show_id)


def set_show_autoplay(conn: sqlite3.Connection, show_id: int, autoplay: bool) -> None:  # LM-4
    _update_one(conn, "UPDATE show SET autoplay = ? WHERE id = ?", (int(autoplay), show_id), show_id)


def rename_episode(conn: sqlite3.Connection, episode_id: int, title: str) -> None:  # LM-1
    title = title.strip()
    if not title:
        raise ValueError("episode title must not be empty")
    _update_one(conn, "UPDATE episode SET title = ? WHERE id = ?", (title, episode_id), episode_id)


def move_episode(conn: sqlite3.Connection, episode_id: int, to_show_id: int) -> None:
    """Move an episode to the end of another show (LM-1).

    If its source video has no other episode left in the old show, the source
    follows the episode so its files stay grouped with the show that uses them.
    """
    with _transaction(conn):
        ep = conn.execute("SELECT show_id, source_video_id FROM episode WHERE id = ?", (episode_id,)).fetchone()
        if ep is None:
            raise KeyError(episode_id)
        if conn.execute("SELECT 1 FROM show WHERE id = ?", (to_show_id,)).fetchone() is None:
            raise KeyError(to_show_id)
        old_show_id, source_video_id = ep["show_id"], ep["source_video_id"]
        base = conn.execute(
            "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM episode WHERE show_id = ?", (to_show_id,)
        ).fetchone()[0]
        conn.execute("UPDATE episode SET show_id = ?, sort_order = ? WHERE id = ?", (to_show_id, base, episode_id))
        if source_video_id is not None and old_show_id != to_show_id:
            remaining = conn.execute(
                "SELECT 1 FROM episode WHERE show_id = ? AND source_video_id = ? LIMIT 1",
                (old_show_id, source_video_id),
            ).fetchone()
            if remaining is None:
                conn.execute("UPDATE source_video SET show_id = ? WHERE id = ?", (to_show_id, source_video_id))


def move_episode_step(conn: sqlite3.Connection, episode_id: int, direction: str) -> None:
    """Swap an episode with its neighbour by (sort_order, id); a no-op at either end (LM-1).

    Ties are normalised to 0..n-1 first, so equal sort_order values never cause a swap
    to land on the wrong neighbour.
    """
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    with _transaction(conn):
        row = conn.execute("SELECT show_id FROM episode WHERE id = ?", (episode_id,)).fetchone()
        if row is None:
            raise KeyError(episode_id)
        show_id = row["show_id"]
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM episode WHERE show_id = ? ORDER BY sort_order, id", (show_id,)
        ).fetchall()]
        conn.executemany("UPDATE episode SET sort_order = ? WHERE id = ?", [(i, eid) for i, eid in enumerate(ids)])
        idx = ids.index(episode_id)
        other = idx - 1 if direction == "up" else idx + 1
        if other < 0 or other >= len(ids):
            return
        conn.execute("UPDATE episode SET sort_order = ? WHERE id = ?", (other, episode_id))
        conn.execute("UPDATE episode SET sort_order = ? WHERE id = ?", (idx, ids[other]))


# --- Media files on disk (CI-6) ---

# Every column that references a file under the media dir.
_FILE_REFS = (
    ("episode", "file_path"),
    ("episode", "thumbnail_path"),
    ("source_video", "file_path"),
    ("source_video", "thumbnail_path"),
    ("show", "artwork_path"),
    ("profile", "picture_path"),
)


def _media_file(media_dir: Path, rel: str) -> Path | None:
    """Resolved path of `rel` if it lies inside media_dir (symlinks followed), else None."""
    root = media_dir.resolve()
    path = (root / rel).resolve()
    if path == root or not path.is_relative_to(root):
        log.warning("ignoring media path outside the media dir: %r", rel)
        return None
    return path


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    per_show: dict[int, int]  # every show, 0 if it has no files


def disk_usage(conn: sqlite3.Connection, media_dir: Path) -> DiskUsage:
    """Bytes on disk per show and in total (CI-6). A file referenced twice counts once."""
    per_show = {row["id"]: 0 for row in conn.execute("SELECT id FROM show")}
    rows = conn.execute(
        """SELECT show_id, file_path AS p FROM episode
           UNION ALL SELECT show_id, thumbnail_path FROM episode
           UNION ALL SELECT show_id, file_path FROM source_video
           UNION ALL SELECT show_id, thumbnail_path FROM source_video
           UNION ALL SELECT id, artwork_path FROM show"""
    ).fetchall()
    seen: set[Path] = set()
    total = 0
    for row in rows:
        if not row["p"]:
            continue
        path = _media_file(media_dir, row["p"])
        if path is None or path in seen:
            continue
        seen.add(path)
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        total += size
        if row["show_id"] is not None:
            per_show[row["show_id"]] = per_show.get(row["show_id"], 0) + size
    return DiskUsage(total_bytes=total, per_show=per_show)


@dataclass(frozen=True)
class ShowListItem:
    show: Show
    episode_count: int
    hidden_episode_count: int
    disk_bytes: int


def list_shows_for_admin(conn: sqlite3.Connection, media_dir: Path) -> tuple[list[ShowListItem], int]:
    """Every show in admin order with episode counts and disk use, and the grand total (CI-4, CI-6, LM-3, LM-4)."""
    usage = disk_usage(conn, media_dir)
    rows = conn.execute(f"SELECT {_SHOW_COLS} FROM show ORDER BY sort_order, id").fetchall()
    counts = {
        row["show_id"]: (row["n"], row["hidden_n"])
        for row in conn.execute(
            "SELECT show_id, COUNT(*) AS n, COALESCE(SUM(hidden), 0) AS hidden_n FROM episode GROUP BY show_id"
        )
    }
    items = []
    for row in rows:
        show = _show(row)
        n, hidden_n = counts.get(show.id, (0, 0))
        items.append(ShowListItem(show=show, episode_count=n, hidden_episode_count=hidden_n,
                                  disk_bytes=usage.per_show.get(show.id, 0)))
    return items, usage.total_bytes


def _referenced(conn: sqlite3.Connection, rel: str) -> bool:
    return any(
        conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (rel,)).fetchone()
        for table, col in _FILE_REFS
    )


def _remove_unreferenced(conn: sqlite3.Connection, media_dir: Path, candidates: list[str]) -> None:
    """Delete candidate files no row references any more; prune emptied directories."""
    root = media_dir.resolve()
    for rel in dict.fromkeys(c for c in candidates if c):
        if _referenced(conn, rel):
            continue
        path = _media_file(media_dir, rel)
        if path is None:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("could not delete %s", path)
            continue
        parent = path.parent
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()  # only succeeds when empty
            except OSError:
                break
            parent = parent.parent


def set_show_artwork(conn: sqlite3.Connection, media_dir: Path, show_id: int, rel_path: str) -> None:  # LM-2
    """Point the show at a freshly saved artwork file, then drop the old one if now unused."""
    row = conn.execute("SELECT artwork_path FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise KeyError(show_id)
    old = row["artwork_path"]
    conn.execute("UPDATE show SET artwork_path = ? WHERE id = ?", (rel_path, show_id))
    if old and old != rel_path:
        _remove_unreferenced(conn, media_dir, [old])


def set_episode_thumbnail(conn: sqlite3.Connection, media_dir: Path, episode_id: int, rel_path: str) -> None:  # LM-2
    """Point the episode at a freshly saved thumbnail file, then drop the old one if now unused."""
    row = conn.execute("SELECT thumbnail_path FROM episode WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        raise KeyError(episode_id)
    old = row["thumbnail_path"]
    conn.execute("UPDATE episode SET thumbnail_path = ? WHERE id = ?", (rel_path, episode_id))
    if old and old != rel_path:
        _remove_unreferenced(conn, media_dir, [old])


def set_profile_picture(conn: sqlite3.Connection, media_dir: Path, profile_id: int, rel_path: str | None) -> None:  # PR-1
    """Point the profile at a freshly saved photo (or None to remove it), then drop the old file if unused."""
    row = conn.execute("SELECT picture_path FROM profile WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise KeyError(profile_id)
    old = row["picture_path"]
    conn.execute("UPDATE profile SET picture_path = ? WHERE id = ?", (rel_path, profile_id))
    if old and old != rel_path:
        _remove_unreferenced(conn, media_dir, [old])


def delete_profile(conn: sqlite3.Connection, media_dir: Path, profile_id: int) -> None:  # PR-1
    """Delete a profile with its positions and history links (cascade) and its photo file."""
    row = conn.execute("SELECT picture_path FROM profile WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise KeyError(profile_id)
    conn.execute("DELETE FROM profile WHERE id = ?", (profile_id,))
    if row["picture_path"]:
        _remove_unreferenced(conn, media_dir, [row["picture_path"]])


def _orphan_sources(conn: sqlite3.Connection, source_ids: set[int]) -> list[sqlite3.Row]:
    """Source videos among source_ids that no episode refers to any more."""
    rows = []
    for sid in source_ids:
        if conn.execute("SELECT 1 FROM episode WHERE source_video_id = ? LIMIT 1", (sid,)).fetchone() is None:
            row = conn.execute("SELECT id, file_path, thumbnail_path FROM source_video WHERE id = ?", (sid,)).fetchone()
            if row:
                rows.append(row)
    return rows


def delete_episode(conn: sqlite3.Connection, media_dir: Path, episode_id: int) -> None:
    """Delete an episode with its files (CI-6).

    Its source video goes too once no episode uses it, since in v1 the episode
    and the source share one MP4. Files still referenced elsewhere are kept.
    """
    with _transaction(conn):
        ep = conn.execute(
            "SELECT file_path, thumbnail_path, source_video_id FROM episode WHERE id = ?", (episode_id,)
        ).fetchone()
        if ep is None:
            raise KeyError(episode_id)
        conn.execute("DELETE FROM episode WHERE id = ?", (episode_id,))
        candidates = [ep["file_path"], ep["thumbnail_path"]]
        if ep["source_video_id"] is not None:
            for src in _orphan_sources(conn, {ep["source_video_id"]}):
                conn.execute("DELETE FROM source_video WHERE id = ?", (src["id"],))
                candidates += [src["file_path"], src["thumbnail_path"]]
    _remove_unreferenced(conn, media_dir, candidates)


def delete_show(conn: sqlite3.Connection, media_dir: Path, show_id: int) -> None:
    """Delete a show, its episodes and source videos, and their files (CI-6)."""
    with _transaction(conn):
        show = conn.execute("SELECT artwork_path FROM show WHERE id = ?", (show_id,)).fetchone()
        if show is None:
            raise KeyError(show_id)
        eps = conn.execute(
            "SELECT file_path, thumbnail_path, source_video_id FROM episode WHERE show_id = ?", (show_id,)
        ).fetchall()
        sources = conn.execute("SELECT id FROM source_video WHERE show_id = ?", (show_id,)).fetchall()
        conn.execute("DELETE FROM episode WHERE show_id = ?", (show_id,))
        conn.execute("DELETE FROM show WHERE id = ?", (show_id,))
        # Sources filed under this show or used by its episodes, unless another show's episode uses them.
        linked = {s["id"] for s in sources} | {e["source_video_id"] for e in eps if e["source_video_id"] is not None}
        doomed = {s["id"]: s for s in _orphan_sources(conn, linked)}
        conn.executemany("DELETE FROM source_video WHERE id = ?", [(sid,) for sid in doomed])
        candidates = [show["artwork_path"]]
        candidates += [p for e in eps for p in (e["file_path"], e["thumbnail_path"])]
        candidates += [p for s in doomed.values() for p in (s["file_path"], s["thumbnail_path"])]
    _remove_unreferenced(conn, media_dir, candidates)


# --- Held downloads (CI-4, LM-3): approved but not yet shown to the kid app ---


@dataclass(frozen=True)
class HeldDownload:
    source_id: int
    title: str
    status: str  # source_video.status: queued | downloading | processing | ready | failed
    error: str | None
    episode_id: int | None  # None until the download has been processed and published
    thumbnail_path: str | None
    playlist_id: str | None = None  # CI-7: set when added from a playlist; the library groups by it
    playlist_title: str | None = None


def list_held_downloads(conn: sqlite3.Connection) -> list[HeldDownload]:
    """Source videos added with 'hold' (ingest._publish), oldest first. Not-yet-ready ones show only their status."""
    rows = conn.execute(
        """SELECT sv.id, sv.title, sv.status, sv.error, sv.thumbnail_path, sv.playlist_id, sv.playlist_title,
                  e.id AS episode_id
           FROM source_video sv LEFT JOIN episode e ON e.source_video_id = sv.id
           WHERE sv.publish = 'hold' ORDER BY sv.id"""
    ).fetchall()
    return [
        HeldDownload(source_id=r["id"], title=r["title"], status=r["status"], error=r["error"],
                     episode_id=r["episode_id"], thumbnail_path=r["thumbnail_path"],
                     playlist_id=r["playlist_id"], playlist_title=r["playlist_title"])
        for r in rows
    ]


def count_held_ready(conn: sqlite3.Connection) -> int:
    """Held downloads that finished and wait for the admin to publish them (admin API, HA-2)."""
    return conn.execute(
        "SELECT COUNT(*) FROM source_video WHERE publish = 'hold' AND status = 'ready'"
    ).fetchone()[0]


def publish_held_playlist(conn: sqlite3.Connection, playlist_id: str, *, now: datetime) -> int:
    """Publish every held video of this playlist that is ready (CI-7). Returns how many; KeyError if none are held.

    Videos still downloading (or failed) stay held; the admin can publish them later.
    """
    with _transaction(conn):
        rows = conn.execute(
            "SELECT id, status FROM source_video WHERE playlist_id = ? AND publish = 'hold'", (playlist_id,)
        ).fetchall()
        if not rows:
            raise KeyError(playlist_id)
        ready = [r["id"] for r in rows if r["status"] == "ready"]
        for source_video_id in ready:
            publish_held(conn, source_video_id, now=now)  # joins this transaction
    return len(ready)


def publish_held(conn: sqlite3.Connection, source_video_id: int, *, now: datetime) -> None:
    """Unhide a held download's episode(s) and mark the source published (LM-3)."""
    with _transaction(conn):
        row = conn.execute(
            "SELECT 1 FROM source_video WHERE id = ? AND publish = 'hold'", (source_video_id,)
        ).fetchone()
        if row is None:
            raise KeyError(source_video_id)
        conn.execute(
            "UPDATE source_video SET publish = 'publish', updated_at = ? WHERE id = ?",
            (to_db(now), source_video_id),
        )
        conn.execute("UPDATE episode SET hidden = 0 WHERE source_video_id = ?", (source_video_id,))
