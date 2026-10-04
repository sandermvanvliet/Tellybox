"""Channel subscriptions and the approval inbox (CS-1..CS-9, HA-9).

A subscription watches a channel's Videos tab (and Shorts when its toggle is on). A check turns
uploads it hasn't seen into pending inbox items; nothing downloads or reaches the kid app until
the admin approves an item (A-9). The baseline is a set of seen video ids, not a date (A-32).

Everything takes the connection and `now` explicitly, and a ChannelLister for anything that
touches YouTube, so tests use a fake. No web code lives here.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tellybox import ingest, library
from tellybox.db import from_db, to_db
from tellybox.ytdlp import ChannelEntry, ChannelLister, ChannelTab, VideoInfo, YtDlpError

log = logging.getLogger(__name__)

PAGE_SIZE = 30  # CS-1, CS-2: one listing page
CHECK_CAP = 100  # CS-2: a check pages on while a whole page is new, up to this many entries
BASELINE_SIZE = 100  # A-32: the newest ids remembered when subscribing
UNHEALTHY_AFTER = timedelta(days=7)  # CS-7
MIN_CHECK_HOURS = 1  # CS-2
WARNING_NOT_DOWNLOADABLE = "may not be downloadable"  # CS-5; stored as English text, translated where shown
_PUBLIC = (None, "public", "unlisted")
_NOT_PUBLIC_YET = ("is_live", "is_upcoming", "post_live")  # finished streams ("was_live") count (A-31)


class AlreadySubscribed(Exception):
    def __init__(self, subscription_id: int) -> None:
        super().__init__(f"already subscribed (subscription {subscription_id})")
        self.subscription_id = subscription_id


class ItemNotPending(Exception):
    """An inbox decision on an item that isn't in the status the action needs."""

    def __init__(self, item_id: int, status: str) -> None:
        super().__init__(f"inbox item {item_id} is {status}")
        self.item_id = item_id
        self.status = status


@dataclass(frozen=True)
class Subscription:
    id: int
    channel_id: str
    channel_name: str
    channel_url: str
    show_id: int | None
    include_shorts: bool
    paused: bool
    baseline_at: datetime
    last_checked_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None
    failing_since: datetime | None
    created_at: datetime
    unhealthy: bool = False  # CS-7: failing for 7 days; set when `now` is given


@dataclass(frozen=True)
class InboxItem:
    id: int
    subscription_id: int | None
    youtube_id: str
    url: str
    title: str
    channel_name: str | None
    duration_s: float | None
    thumbnail_url: str | None
    published_at: datetime | None
    status: str
    warning: str | None
    received_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class BacklogPage:
    channel_id: str
    channel_name: str
    entries: list[ChannelEntry]  # this page minus videos already in the library
    next_offset: int | None  # None when the listing has no more


@dataclass(frozen=True)
class CheckResult:
    new_item_ids: list[int] = field(default_factory=list)
    skipped_live: int = 0  # live or upcoming: not marked seen, picked up once they are normal videos
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class BulkApproveResult:
    approved: list[tuple[int, int, int]]  # (item_id, source_video_id, job_id)
    skipped: list[int]  # item ids that were unknown, not pending, or already in the library


# --------------------------------------------------------------------------- reading


_SUB_COLUMNS = (
    "id, channel_id, channel_name, channel_url, show_id, include_shorts, paused, baseline_at, last_checked_at,"
    " last_ok_at, last_error, failing_since, created_at"
)


def _subscription(row: sqlite3.Row, now: datetime | None) -> Subscription:
    failing = from_db(row["failing_since"])
    return Subscription(
        id=row["id"], channel_id=row["channel_id"], channel_name=row["channel_name"],
        channel_url=row["channel_url"], show_id=row["show_id"], include_shorts=bool(row["include_shorts"]),
        paused=bool(row["paused"]), baseline_at=from_db(row["baseline_at"]),
        last_checked_at=from_db(row["last_checked_at"]), last_ok_at=from_db(row["last_ok_at"]),
        last_error=row["last_error"], failing_since=failing, created_at=from_db(row["created_at"]),
        unhealthy=bool(now and failing and now - failing >= UNHEALTHY_AFTER),
    )


def _item(row: sqlite3.Row) -> InboxItem:
    return InboxItem(
        id=row["id"], subscription_id=row["subscription_id"], youtube_id=row["youtube_id"], url=row["url"],
        title=row["title"], channel_name=row["channel_name"], duration_s=row["duration_s"],
        thumbnail_url=row["thumbnail_url"], published_at=from_db(row["published_at"]), status=row["status"],
        warning=row["warning"], received_at=from_db(row["received_at"]), decided_at=from_db(row["decided_at"]),
    )


def get_subscription(
    conn: sqlite3.Connection, subscription_id: int, *, now: datetime | None = None
) -> Subscription | None:
    row = conn.execute(f"SELECT {_SUB_COLUMNS} FROM subscription WHERE id = ?", (subscription_id,)).fetchone()
    return _subscription(row, now) if row else None


def list_subscriptions(conn: sqlite3.Connection, *, now: datetime) -> list[Subscription]:
    """All subscriptions by name, each with its last check, last error and the CS-7 warning flag."""
    rows = conn.execute(f"SELECT {_SUB_COLUMNS} FROM subscription ORDER BY channel_name COLLATE NOCASE, id")
    return [_subscription(r, now) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> InboxItem | None:
    row = conn.execute("SELECT * FROM inbox_item WHERE id = ?", (item_id,)).fetchone()
    return _item(row) if row else None


def list_inbox(
    conn: sqlite3.Connection, *, status: str = "pending", subscription_id: int | None = None
) -> list[InboxItem]:
    """Inbox items of one status, newest upload first (CS-8); optionally one subscription's."""
    sql = "SELECT * FROM inbox_item WHERE status = ?"
    args: list = [status]
    if subscription_id is not None:
        sql += " AND subscription_id = ?"
        args.append(subscription_id)
    rows = conn.execute(sql + " ORDER BY COALESCE(published_at, received_at) DESC, id DESC", args)
    return [_item(r) for r in rows]


def inbox_counts(conn: sqlite3.Connection, now: datetime) -> dict:
    """The HA-9 inbox object. Pending counts include paused subscriptions."""
    pending = conn.execute("SELECT COUNT(*) FROM inbox_item WHERE status = 'pending'").fetchone()[0]
    unhealthy = conn.execute(
        "SELECT COUNT(*) FROM subscription WHERE failing_since IS NOT NULL AND failing_since <= ?",
        (to_db(now - UNHEALTHY_AFTER),),
    ).fetchone()[0]
    latest = conn.execute("SELECT MAX(received_at) FROM inbox_item").fetchone()[0]
    return {"pending": pending, "unhealthy": unhealthy, "latest_received_at": latest}


# --------------------------------------------------------------------------- subscribe, pause, remove


def _ids_in(conn: sqlite3.Connection, table: str, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), 500):  # stay well below SQLite's bound-parameter limit
        chunk = ids[i:i + 500]
        found.update(r[0] for r in conn.execute(
            f"SELECT youtube_id FROM {table} WHERE youtube_id IN ({', '.join('?' * len(chunk))})", chunk
        ))
    return found


def _newest_ids(lister: ChannelLister, url: str, tab: ChannelTab, first: list[ChannelEntry]) -> list[str]:
    """The ids of the newest BASELINE_SIZE entries of a tab; `first` is its already fetched first page."""
    ids = [e.youtube_id for e in first]
    full = len(first) == PAGE_SIZE
    while full and len(ids) < BASELINE_SIZE:
        limit = min(PAGE_SIZE, BASELINE_SIZE - len(ids))
        page = lister.list_channel(url, tab, len(ids), limit).entries
        ids += [e.youtube_id for e in page]
        full = len(page) == limit
    return ids


def subscribe(
    conn: sqlite3.Connection, lister: ChannelLister, url: str, *, show_id: int | None = None,
    include_shorts: bool = False, now: datetime,
) -> Subscription:
    """CS-1: subscribe to a channel and take the baseline.

    The newest 100 ids of the Videos tab (and of Shorts, when included) are remembered as seen, so only
    later uploads become inbox items (A-32). The channel is keyed on its stable id; a bad URL raises
    the lister's YtDlpError. Raises AlreadySubscribed for a channel already subscribed to.
    """
    first = lister.list_channel(url, "videos", 0, PAGE_SIZE)
    existing = conn.execute("SELECT id FROM subscription WHERE channel_id = ?", (first.channel_id,)).fetchone()
    if existing:
        raise AlreadySubscribed(existing["id"])
    channel_url = f"https://www.youtube.com/channel/{first.channel_id}"
    seen = _newest_ids(lister, channel_url, "videos", first.entries)
    if include_shorts:
        shorts = lister.list_channel(channel_url, "shorts", 0, PAGE_SIZE).entries
        seen += _newest_ids(lister, channel_url, "shorts", shorts)
    with library._transaction(conn):
        cur = conn.execute(
            """INSERT INTO subscription (channel_id, channel_name, channel_url, show_id, include_shorts, baseline_at,
                 created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (first.channel_id, first.channel_name, channel_url, show_id, int(include_shorts), to_db(now), to_db(now)),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO subscription_seen (subscription_id, youtube_id) VALUES (?, ?)",
            [(cur.lastrowid, vid) for vid in seen],
        )
    log.info("subscribed to %s (%s), baseline of %d videos", first.channel_name, first.channel_id, len(seen))
    return get_subscription(conn, cur.lastrowid, now=now)


def list_backlog(
    conn: sqlite3.Connection, lister: ChannelLister, subscription_or_url: Subscription | str, offset: int,
    limit: int = PAGE_SIZE, *, tab: ChannelTab = "videos",
) -> BacklogPage:
    """CS-1: one page of a channel's existing uploads for selection. Read-only.

    Videos already in the library are left out. `offset` counts listed entries, so the next page
    starts at next_offset whatever was filtered out.
    """
    url = subscription_or_url.channel_url if isinstance(subscription_or_url, Subscription) else subscription_or_url
    listing = lister.list_channel(url, tab, offset, limit)
    known = ingest.existing_youtube_ids(conn, [e.youtube_id for e in listing.entries])
    return BacklogPage(
        listing.channel_id, listing.channel_name, [e for e in listing.entries if e.youtube_id not in known],
        offset + limit if len(listing.entries) >= limit else None,
    )


def _must_exist(conn: sqlite3.Connection, subscription_id: int) -> None:
    if not conn.execute("SELECT 1 FROM subscription WHERE id = ?", (subscription_id,)).fetchone():
        raise KeyError(subscription_id)


def pause(conn: sqlite3.Connection, subscription_id: int) -> None:
    """CS-4: checks stop; the inbox stays visible and actionable."""
    _must_exist(conn, subscription_id)
    conn.execute("UPDATE subscription SET paused = 1 WHERE id = ?", (subscription_id,))


def resume(conn: sqlite3.Connection, subscription_id: int) -> None:
    """CS-4: the seen set wasn't touched while paused, so the next check finds the uploads made meanwhile."""
    _must_exist(conn, subscription_id)
    conn.execute("UPDATE subscription SET paused = 0 WHERE id = ?", (subscription_id,))


def remove(conn: sqlite3.Connection, subscription_id: int) -> None:
    """CS-4: delete the subscription, its seen set and its pending items.

    Approved and rejected items stay with subscription_id NULL, so rejections are remembered (A-34).
    The show and its episodes are not touched (A-6).
    """
    _must_exist(conn, subscription_id)
    with library._transaction(conn):
        conn.execute("DELETE FROM inbox_item WHERE subscription_id = ? AND status = 'pending'", (subscription_id,))
        conn.execute("DELETE FROM subscription WHERE id = ?", (subscription_id,))  # seen rows cascade


# --------------------------------------------------------------------------- checks


def _scan_tab(lister: ChannelLister, sub: Subscription, tab: ChannelTab, seen: set[str]) -> list[ChannelEntry]:
    """Entries of a tab not in the seen set: the first page, and more while a whole page is unseen (CS-2)."""
    unseen: list[ChannelEntry] = []
    offset = 0
    while True:
        limit = min(PAGE_SIZE, CHECK_CAP - offset)
        page = lister.list_channel(sub.channel_url, tab, offset, limit).entries
        fresh = [e for e in page if e.youtube_id not in seen]
        unseen += fresh
        offset += limit
        if len(page) < limit or len(fresh) < len(page):
            return unseen  # the end of the listing, or a seen id: everything older is known
        if offset >= CHECK_CAP:
            log.warning("subscription %d (%s): all of the newest %d %s are new; not looking further",
                        sub.id, sub.channel_name, CHECK_CAP, tab)
            return unseen


def check(conn: sqlite3.Connection, lister: ChannelLister, subscription_id: int, *, now: datetime) -> CheckResult:
    """CS-2, CS-5, CS-7: look for new uploads and put them in the inbox as pending items.

    A candidate is a listed id that isn't in the seen set, the library or the inbox (any status).
    Live and upcoming videos make no item and aren't marked seen, so a later check finds them once
    they are normal videos. Members-only and age-restricted ones get an item with a warning.
    Nothing is written unless the whole listing succeeded; a failure is stored on the subscription
    and never raised (A-35). Paused subscriptions can still be checked; the caller decides.
    """
    sub = get_subscription(conn, subscription_id, now=now)
    if sub is None:
        raise KeyError(subscription_id)
    seen = {r[0] for r in conn.execute("SELECT youtube_id FROM subscription_seen WHERE subscription_id = ?", (sub.id,))}
    items: list[tuple] = []
    mark_seen: list[str] = []
    skipped_live = 0
    try:
        tabs: list[ChannelTab] = ["videos", "shorts"] if sub.include_shorts else ["videos"]
        candidates = [e for tab in tabs for e in _scan_tab(lister, sub, tab, seen)]
        ids = [e.youtube_id for e in candidates]
        known = _ids_in(conn, "source_video", ids) | _ids_in(conn, "inbox_item", ids)
        for entry in candidates:
            if entry.youtube_id in known:
                mark_seen.append(entry.youtube_id)  # in the library, pending or rejected: skipped silently (CS-5)
                continue
            if entry.live_status in _NOT_PUBLIC_YET or (not entry.is_short and entry.duration_s is None):
                skipped_live += 1
                continue
            status = lister.video_status(entry.youtube_id)
            if status.live_status in _NOT_PUBLIC_YET:
                skipped_live += 1
                continue
            items.append((
                sub.id, entry.youtube_id, entry.url, entry.title, sub.channel_name, entry.duration_s,
                entry.thumbnail_url, to_db(status.published_at or entry.approx_date),
                None if status.availability in _PUBLIC else WARNING_NOT_DOWNLOADABLE, to_db(now),
            ))
            mark_seen.append(entry.youtube_id)
    except Exception as exc:  # a failed check is stored and retried on schedule, never raised (A-35)
        if isinstance(exc, YtDlpError):
            message = exc.message
            log.warning("check of subscription %d (%s) failed: %s", sub.id, sub.channel_name, message)
        else:
            message = f"{type(exc).__name__}: {exc}"
            log.exception("check of subscription %d failed", sub.id)
        conn.execute(
            """UPDATE subscription SET last_checked_at = ?, last_error = ?,
                 failing_since = COALESCE(failing_since, ?) WHERE id = ?""",
            (to_db(now), message, to_db(now), sub.id),
        )
        return CheckResult(error=message)
    new_ids: list[int] = []
    with library._transaction(conn):
        for row in items:
            cur = conn.execute(
                """INSERT OR IGNORE INTO inbox_item (subscription_id, youtube_id, url, title, channel_name, duration_s,
                     thumbnail_url, published_at, warning, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )
            if cur.rowcount:
                new_ids.append(cur.lastrowid)
        conn.executemany(
            "INSERT OR IGNORE INTO subscription_seen (subscription_id, youtube_id) VALUES (?, ?)",
            [(sub.id, vid) for vid in mark_seen],
        )
        conn.execute(
            """UPDATE subscription SET last_checked_at = ?, last_ok_at = ?, last_error = NULL, failing_since = NULL
               WHERE id = ?""",
            (to_db(now), to_db(now), sub.id),
        )
    if new_ids:
        log.info("subscription %d (%s): %d new videos in the inbox", sub.id, sub.channel_name, len(new_ids))
    return CheckResult(new_item_ids=new_ids, skipped_live=skipped_live)


# --------------------------------------------------------------------------- decisions


def approve(conn: sqlite3.Connection, item_id: int, *, now: datetime) -> tuple[int, int]:
    """CS-3: approve a pending item. Returns (source_video_id, job_id).

    One transaction: the download is queued with publish, in the subscription's show (CS-9), and the
    item becomes approved. Raises KeyError for an unknown item and ItemNotPending for any other status.
    A video that is already in the library marks the item approved and raises ingest.AlreadyAdded.
    """
    already: ingest.AlreadyAdded | None = None
    result: tuple[int, int] | None = None
    with library._transaction(conn):
        row = conn.execute(
            """SELECT i.*, s.show_id AS sub_show_id, s.channel_id AS sub_channel_id
               FROM inbox_item i LEFT JOIN subscription s ON s.id = i.subscription_id WHERE i.id = ?""",
            (item_id,),
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        if row["status"] != "pending":
            raise ItemNotPending(item_id, row["status"])
        info = VideoInfo(
            youtube_id=row["youtube_id"], url=row["url"], title=row["title"], channel_id=row["sub_channel_id"],
            channel_name=row["channel_name"], duration_s=row["duration_s"], thumbnail_url=row["thumbnail_url"],
            chapters=[], is_live=False,
        )
        try:
            result = ingest.add(conn, info, publish=True, now=now, show_id=row["sub_show_id"])
        except ingest.AlreadyAdded as exc:
            already = exc
        conn.execute("UPDATE inbox_item SET status = 'approved', decided_at = ? WHERE id = ?", (to_db(now), item_id))
    if already:
        raise already
    return result


def reject(conn: sqlite3.Connection, item_id: int, *, now: datetime) -> None:
    """CS-6: reject a pending item. The id is remembered, so it never comes back (A-34)."""
    _decide(conn, item_id, "pending", "rejected", now)


def undo_reject(conn: sqlite3.Connection, item_id: int, *, now: datetime) -> None:
    """CS-6: a rejected item is pending again."""
    _decide(conn, item_id, "rejected", "pending", now)


def _decide(conn: sqlite3.Connection, item_id: int, expected: str, new: str, now: datetime) -> None:
    with library._transaction(conn):
        row = conn.execute("SELECT status FROM inbox_item WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        if row["status"] != expected:
            raise ItemNotPending(item_id, row["status"])
        conn.execute(
            "UPDATE inbox_item SET status = ?, decided_at = ? WHERE id = ?",
            (new, to_db(now) if new != "pending" else None, item_id),
        )


def bulk_approve(conn: sqlite3.Connection, item_ids: list[int], *, now: datetime) -> BulkApproveResult:
    """Approve each pending item in the list, one transaction apiece; the others are skipped, not errors."""
    approved: list[tuple[int, int, int]] = []
    skipped: list[int] = []
    for item_id in dict.fromkeys(item_ids):
        try:
            source_id, job_id = approve(conn, item_id, now=now)
        except (KeyError, ItemNotPending, ingest.AlreadyAdded):
            skipped.append(item_id)
        else:
            approved.append((item_id, source_id, job_id))
    return BulkApproveResult(approved, skipped)


def bulk_reject(conn: sqlite3.Connection, item_ids: list[int], *, now: datetime) -> int:
    """Reject each pending item in the list; returns how many were rejected."""
    count = 0
    for item_id in dict.fromkeys(item_ids):
        try:
            reject(conn, item_id, now=now)
        except (KeyError, ItemNotPending):
            continue
        count += 1
    return count


# --------------------------------------------------------------------------- schedule


def get_check_hours(conn: sqlite3.Connection) -> int:
    """CS-2: the global check interval in hours (default 6)."""
    return conn.execute("SELECT subscription_check_hours FROM settings WHERE id = 1").fetchone()[0]


def set_check_hours(conn: sqlite3.Connection, hours: int) -> None:
    """CS-2: set the interval; ValueError below the 1 hour minimum."""
    if hours < MIN_CHECK_HOURS:
        raise ValueError(f"the check interval is at least {MIN_CHECK_HOURS} hour")
    conn.execute("UPDATE settings SET subscription_check_hours = ? WHERE id = 1", (int(hours),))
