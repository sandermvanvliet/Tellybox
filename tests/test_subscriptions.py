"""Channel subscriptions and the approval inbox (CS-1..CS-9, HA-9, A-31..A-36). Fake lister and clock; no network."""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from tellybox import ingest, jobs, library, subscriptions
from tellybox.clock import FakeClock
from tellybox.db import open_db, to_db
from tellybox.jobs import JobType
from tellybox.ytdlp import VideoInfo, YtDlpError
from tests.channel_fakes import FakeChannelLister, entry

CHANNEL = "UCkids"
URL = "https://www.youtube.com/@kidsfun"


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def conn():
    return open_db(":memory:")


@pytest.fixture
def lister():
    fake = FakeChannelLister()
    # newest first: old000 is the most recent of 40 existing uploads
    fake.add_channel(CHANNEL, "Kids Fun", handle="@kidsfun", videos=[f"old{i:03d}" for i in range(40)],
                     shorts=["oldshort0", "oldshort1"])
    return fake


@pytest.fixture
def sub(conn, lister, clock):
    return subscriptions.subscribe(conn, lister, URL, now=clock.now())


def pending_ids(conn):
    return [i.youtube_id for i in subscriptions.list_inbox(conn)]


def seen_count(conn, sub_id):
    return conn.execute("SELECT COUNT(*) FROM subscription_seen WHERE subscription_id = ?", (sub_id,)).fetchone()[0]


# --- subscribe and baseline (CS-1, A-32) -------------------------------------


def test_subscribe_resolves_the_channel_id_and_stores_the_baseline(conn, lister, clock):
    sub = subscriptions.subscribe(conn, lister, URL, now=clock.now())
    assert (sub.channel_id, sub.channel_name) == (CHANNEL, "Kids Fun")
    assert sub.channel_url == f"https://www.youtube.com/channel/{CHANNEL}"  # the id is the key, not the handle
    assert sub.baseline_at == clock.now() and not sub.paused and not sub.include_shorts and sub.show_id is None
    assert seen_count(conn, sub.id) == 40  # every existing video, here fewer than 100
    assert subscriptions.list_inbox(conn) == []


def test_baseline_is_the_newest_100_ids_paged_in_30s(conn, lister, clock):
    lister.add_channel("UCbig", "Big", videos=[f"big{i:03d}" for i in range(250)])
    sub = subscriptions.subscribe(conn, lister, "https://www.youtube.com/channel/UCbig", now=clock.now())
    assert seen_count(conn, sub.id) == 100
    assert [(o, n) for _, _, o, n in lister.calls] == [(0, 30), (30, 30), (60, 30), (90, 10)]
    assert conn.execute("SELECT 1 FROM subscription_seen WHERE youtube_id = 'big099'").fetchone()
    assert not conn.execute("SELECT 1 FROM subscription_seen WHERE youtube_id = 'big100'").fetchone()


def test_baseline_includes_shorts_only_when_asked(conn, lister, clock):
    plain = subscriptions.subscribe(conn, lister, URL, now=clock.now())
    assert not conn.execute(
        "SELECT 1 FROM subscription_seen WHERE subscription_id = ? AND youtube_id = 'oldshort0'", (plain.id,)
    ).fetchone()
    subscriptions.remove(conn, plain.id)
    with_shorts = subscriptions.subscribe(conn, lister, URL, include_shorts=True, now=clock.now())
    assert with_shorts.include_shorts and seen_count(conn, with_shorts.id) == 42


def test_subscribe_twice_is_refused(conn, lister, clock, sub):
    with pytest.raises(subscriptions.AlreadySubscribed) as exc:
        subscriptions.subscribe(conn, lister, f"https://www.youtube.com/channel/{CHANNEL}", now=clock.now())
    assert exc.value.subscription_id == sub.id


def test_subscribe_to_a_bad_channel_raises_the_listers_error(conn, lister, clock):
    with pytest.raises(YtDlpError, match="not found"):
        subscriptions.subscribe(conn, lister, "https://www.youtube.com/@nobody", now=clock.now())
    assert subscriptions.list_subscriptions(conn, now=clock.now()) == []


def test_subscribe_keeps_the_chosen_show(conn, lister, clock):
    show_id = library.create_show(conn, "My show", now=clock.now())
    sub = subscriptions.subscribe(conn, lister, URL, show_id=show_id, now=clock.now())
    assert sub.show_id == show_id


def test_backlog_pages_by_30_and_leaves_out_library_videos(conn, lister, clock, sub):
    ingest.add(conn, VideoInfo("old001", "u", "t", CHANNEL, "Kids Fun", 5, None, [], False), publish=True, now=clock.now())
    page = subscriptions.list_backlog(conn, lister, sub, 0)
    assert len(page.entries) == 29 and "old001" not in [e.youtube_id for e in page.entries]
    assert page.next_offset == 30 and page.channel_name == "Kids Fun"
    last = subscriptions.list_backlog(conn, lister, URL, 30)  # a URL works before subscribing
    assert len(last.entries) == 10 and last.next_offset is None
    assert subscriptions.list_inbox(conn) == []  # read-only: the backlog never reaches the inbox


# --- checks (CS-2, CS-5) ------------------------------------------------------


def test_backlog_never_reaches_the_inbox_but_later_uploads_do(conn, lister, clock, sub):
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert result.ok and result.new_item_ids == [] and pending_ids(conn) == []
    lister.upload(CHANNEL, "new1")
    clock.advance(hours=6)
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert len(result.new_item_ids) == 1
    (item,) = subscriptions.list_inbox(conn)
    assert (item.youtube_id, item.status, item.subscription_id, item.warning) == ("new1", "pending", sub.id, None)
    assert item.title == "Video new1" and item.channel_name == "Kids Fun" and item.duration_s == 300
    assert item.received_at == clock.now() and item.published_at is not None  # the exact date from video_status
    assert conn.execute("SELECT COUNT(*) FROM source_video").fetchone()[0] == 0  # nothing downloads (A-9)
    assert subscriptions.check(conn, lister, sub.id, now=clock.now()).new_item_ids == []  # not found twice
    assert lister.status_calls == ["new1"]  # one metadata call, only for the new id


def test_check_records_success_and_stops_at_the_first_page_when_it_has_a_seen_id(conn, lister, clock, sub):
    lister.calls.clear()
    clock.advance(hours=1)
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert len(lister.calls) == 1  # one page, it holds seen ids
    got = subscriptions.get_subscription(conn, sub.id)
    assert got.last_checked_at == got.last_ok_at == clock.now() and got.last_error is None


def test_dedupe_library_pending_rejected(conn, lister, clock, sub):
    ingest.add(conn, VideoInfo("inlib", "u", "t", None, None, 5, None, [], False), publish=True, now=clock.now())
    for vid in ("inlib", "pend", "rej"):
        lister.upload(CHANNEL, vid)
    conn.execute(
        "INSERT INTO inbox_item (youtube_id, url, title, status, received_at) VALUES ('rej', 'u', 't', 'rejected', ?)",
        (to_db(clock.now()),),
    )
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert set(pending_ids(conn)) == {"pend"}
    assert lister.status_calls == ["pend"]  # no metadata call for the known ones
    lister.upload(CHANNEL, "newer")
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert subscriptions.check(conn, lister, sub.id, now=clock.now()).new_item_ids == []  # nothing comes back


def test_same_video_from_two_subscriptions_makes_one_item(conn, lister, clock, sub):
    lister.add_channel("UCother", "Other", videos=["x0", "x1"])
    other = subscriptions.subscribe(conn, lister, "https://www.youtube.com/channel/UCother", now=clock.now())
    lister.upload(CHANNEL, "shared")
    lister.upload("UCother", "shared")
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    result = subscriptions.check(conn, lister, other.id, now=clock.now())
    assert pending_ids(conn) == ["shared"] and result.new_item_ids == []


def test_upcoming_and_live_make_no_item_until_they_are_normal_videos(conn, lister, clock, sub):
    lister.upload(CHANNEL, "premiere")
    lister.set_status("premiere", live_status="is_upcoming")
    lister.upload(CHANNEL, "nodur", duration_s=None)  # no duration in the listing: a live stream
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert result.skipped_live == 2 and pending_ids(conn) == []
    assert lister.status_calls == ["premiere"]  # a missing duration alone is enough to skip
    lister.set_status("premiere", live_status="not_live")
    lister.channels[CHANNEL]["videos"][0] = entry("nodur")  # the stream has finished and has a duration
    clock.advance(hours=6)
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert set(pending_ids(conn)) == {"premiere", "nodur"} and len(result.new_item_ids) == 2


def test_a_finished_livestream_counts(conn, lister, clock, sub):
    lister.upload(CHANNEL, "stream")
    lister.set_status("stream", live_status="was_live")
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert pending_ids(conn) == ["stream"]


@pytest.mark.parametrize("availability", ["subscriber_only", "needs_auth", "premium_only"])
def test_members_only_and_age_restricted_get_a_warning(conn, lister, clock, sub, availability):
    lister.upload(CHANNEL, "locked")
    lister.set_status("locked", availability=availability)
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    (item,) = subscriptions.list_inbox(conn)
    assert item.warning == subscriptions.WARNING_NOT_DOWNLOADABLE and item.status == "pending"


def test_shorts_follow_the_toggle(conn, lister, clock, sub):
    lister.upload(CHANNEL, "short1", short=True)
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert pending_ids(conn) == [] and all(tab == "videos" for _, tab, _, _ in lister.calls)


def _lister_with_shorts():
    fake = FakeChannelLister()
    fake.add_channel("UCshorty", "Shorty", handle="@shorty", videos=["v0"], shorts=["s0"])
    return fake


def test_shorts_are_checked_when_included(conn, clock):
    fake = _lister_with_shorts()
    sub = subscriptions.subscribe(conn, fake, "https://www.youtube.com/@shorty", include_shorts=True, now=clock.now())
    fake.upload("UCshorty", "s1", short=True)
    fake.upload("UCshorty", "v1")
    result = subscriptions.check(conn, fake, sub.id, now=clock.now())
    assert set(pending_ids(conn)) == {"s1", "v1"} and len(result.new_item_ids) == 2
    (short_item,) = [i for i in subscriptions.list_inbox(conn) if i.youtube_id == "s1"]
    assert short_item.duration_s is None  # a Short has none, and is not mistaken for a live stream


# --- paging (CS-2) ----------------------------------------------------------------


def test_paging_continues_while_a_whole_page_is_new_and_stops_at_a_seen_id(conn, lister, clock, sub):
    for i in range(45):  # 45 uploads since the last check: page 1 all new, page 2 holds seen ids
        lister.upload(CHANNEL, f"n{i:02d}")
    lister.calls.clear()
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert len(result.new_item_ids) == 45
    assert [(o, n) for _, _, o, n in lister.calls] == [(0, 30), (30, 30)]


def test_paging_is_capped_at_100_with_a_warning(conn, lister, clock, sub, caplog):
    for i in range(130):
        lister.upload(CHANNEL, f"n{i:03d}")
    lister.calls.clear()
    with caplog.at_level(logging.WARNING, logger="tellybox.subscriptions"):
        result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert len(result.new_item_ids) == 100
    assert [(o, n) for _, _, o, n in lister.calls] == [(0, 30), (30, 30), (60, 30), (90, 10)]
    assert any("not looking further" in r.message for r in caplog.records)


def test_no_warning_when_the_listing_simply_ends(conn, lister, clock, caplog):
    lister.add_channel("UCtiny", "Tiny", videos=[])
    sub = subscriptions.subscribe(conn, lister, "https://www.youtube.com/channel/UCtiny", now=clock.now())
    for i in range(3):
        lister.upload("UCtiny", f"t{i}")
    with caplog.at_level(logging.WARNING, logger="tellybox.subscriptions"):
        assert len(subscriptions.check(conn, lister, sub.id, now=clock.now()).new_item_ids) == 3
    assert not caplog.records


# --- failures (CS-7, A-35) ----------------------------------------------------------


def test_failure_is_stored_not_raised_and_existing_rows_are_untouched(conn, lister, clock, sub):
    lister.upload(CHANNEL, "keep")
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    before = [dict(r) for r in conn.execute("SELECT * FROM inbox_item")]
    seen_before = seen_count(conn, sub.id)
    lister.fail = YtDlpError("HTTP Error 503", retryable=True)
    clock.advance(hours=6)
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert not result.ok and result.error == "HTTP Error 503"
    got = subscriptions.get_subscription(conn, sub.id)
    assert got.last_error == "HTTP Error 503" and got.failing_since == clock.now() and got.last_checked_at == clock.now()
    assert [dict(r) for r in conn.execute("SELECT * FROM inbox_item")] == before and seen_count(conn, sub.id) == seen_before


def test_unexpected_errors_are_stored_too(conn, lister, clock, sub):
    lister.fail = RuntimeError("boom")
    assert subscriptions.check(conn, lister, sub.id, now=clock.now()).error == "RuntimeError: boom"


def test_a_failed_check_adds_nothing_even_after_a_good_first_page(conn, lister, clock, sub):
    for i in range(35):
        lister.upload(CHANNEL, f"n{i:02d}")
    real = lister.list_channel

    def flaky(url, tab, offset, limit):
        if offset:
            raise YtDlpError("second page failed", retryable=True)
        return real(url, tab, offset, limit)

    lister.list_channel = flaky
    assert not subscriptions.check(conn, lister, sub.id, now=clock.now()).ok
    assert pending_ids(conn) == []
    lister.list_channel = real
    assert len(subscriptions.check(conn, lister, sub.id, now=clock.now()).new_item_ids) == 35  # retried in full


def test_failing_since_is_kept_across_failures_and_cleared_by_success(conn, lister, clock, sub):
    lister.fail = YtDlpError("down", retryable=True)
    first = clock.now()
    subscriptions.check(conn, lister, sub.id, now=first)
    clock.advance(hours=6)
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert subscriptions.get_subscription(conn, sub.id).failing_since == first
    lister.fail = None
    clock.advance(hours=6)
    assert subscriptions.check(conn, lister, sub.id, now=clock.now()).ok
    got = subscriptions.get_subscription(conn, sub.id)
    assert got.last_error is None and got.failing_since is None and got.last_ok_at == clock.now()


def test_unhealthy_after_seven_days_of_failures(conn, lister, clock, sub):
    lister.fail = YtDlpError("gone", retryable=True)
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    start = clock.now()
    (row,) = subscriptions.list_subscriptions(conn, now=start + timedelta(days=6, hours=23))
    assert not row.unhealthy and row.last_error == "gone"
    assert subscriptions.inbox_counts(conn, start + timedelta(days=6, hours=23))["unhealthy"] == 0
    (row,) = subscriptions.list_subscriptions(conn, now=start + timedelta(days=7))
    assert row.unhealthy
    assert subscriptions.inbox_counts(conn, start + timedelta(days=7))["unhealthy"] == 1


# --- decisions (CS-3, CS-6, CS-9) -------------------------------------------------------


def make_item(conn, lister, clock, sub, vid="up1"):
    lister.upload(CHANNEL, vid)
    (item_id,) = subscriptions.check(conn, lister, sub.id, now=clock.now()).new_item_ids
    return item_id


def test_approve_queues_a_publishing_download_and_marks_the_item(conn, lister, clock, sub):
    item_id = make_item(conn, lister, clock, sub)
    clock.advance(minutes=5)
    source_id, job_id = subscriptions.approve(conn, item_id, now=clock.now())
    src = ingest.get_source_video(conn, source_id)
    assert (src.youtube_id, src.publish, src.status, src.channel_id, src.show_id) == ("up1", "publish", "queued", CHANNEL, None)
    job = jobs.get(conn, job_id)
    assert (job.type, job.target_id) == (JobType.DOWNLOAD, source_id)
    item = subscriptions.get_item(conn, item_id)
    assert item.status == "approved" and item.decided_at == clock.now()
    assert subscriptions.list_inbox(conn) == [] and [i.id for i in subscriptions.list_inbox(conn, status="approved")] == [item_id]


def test_approve_uses_the_subscriptions_show(conn, lister, clock):
    show_id = library.create_show(conn, "Chosen show", now=clock.now())
    sub = subscriptions.subscribe(conn, lister, URL, show_id=show_id, now=clock.now())
    source_id, _ = subscriptions.approve(conn, make_item(conn, lister, clock, sub), now=clock.now())
    assert ingest.get_source_video(conn, source_id).show_id == show_id


def test_approve_follows_a_renamed_or_merged_show(conn, lister, clock):
    show_id = library.create_show(conn, "Chosen show", now=clock.now())
    into = library.create_show(conn, "Merged into", now=clock.now())
    sub = subscriptions.subscribe(conn, lister, URL, show_id=show_id, now=clock.now())
    library.merge_shows(conn, into, show_id, now=clock.now())
    got = subscriptions.get_subscription(conn, sub.id)
    assert got.show_id == into  # CS-9: the reference follows the merge


def test_double_approve_is_refused(conn, lister, clock, sub):
    item_id = make_item(conn, lister, clock, sub)
    subscriptions.approve(conn, item_id, now=clock.now())
    with pytest.raises(subscriptions.ItemNotPending) as exc:
        subscriptions.approve(conn, item_id, now=clock.now())
    assert exc.value.status == "approved"
    assert conn.execute("SELECT COUNT(*) FROM source_video").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1


def test_approving_a_rejected_or_unknown_item_is_refused(conn, lister, clock, sub):
    item_id = make_item(conn, lister, clock, sub)
    subscriptions.reject(conn, item_id, now=clock.now())
    with pytest.raises(subscriptions.ItemNotPending):
        subscriptions.approve(conn, item_id, now=clock.now())
    with pytest.raises(KeyError):
        subscriptions.approve(conn, 999, now=clock.now())


def test_approve_of_a_video_already_in_the_library_marks_it_approved_and_says_so(conn, lister, clock, sub):
    item_id = make_item(conn, lister, clock, sub)
    ingest.add(conn, VideoInfo("up1", "u", "t", None, None, 5, None, [], False), publish=True, now=clock.now())
    with pytest.raises(ingest.AlreadyAdded):
        subscriptions.approve(conn, item_id, now=clock.now())
    assert subscriptions.get_item(conn, item_id).status == "approved"
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1


def test_a_failed_add_leaves_the_item_pending(conn, lister, clock, sub, monkeypatch):
    item_id = make_item(conn, lister, clock, sub)

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ingest, "add", boom)
    with pytest.raises(RuntimeError):
        subscriptions.approve(conn, item_id, now=clock.now())
    assert subscriptions.get_item(conn, item_id).status == "pending" and not conn.in_transaction


def test_reject_undo_and_the_rejection_is_remembered(conn, lister, clock, sub):
    item_id = make_item(conn, lister, clock, sub)
    clock.advance(minutes=1)
    subscriptions.reject(conn, item_id, now=clock.now())
    item = subscriptions.get_item(conn, item_id)
    assert item.status == "rejected" and item.decided_at == clock.now()
    assert [i.id for i in subscriptions.list_inbox(conn, status="rejected")] == [item_id]
    with pytest.raises(subscriptions.ItemNotPending):
        subscriptions.reject(conn, item_id, now=clock.now())
    subscriptions.undo_reject(conn, item_id, now=clock.now())
    item = subscriptions.get_item(conn, item_id)
    assert item.status == "pending" and item.decided_at is None
    with pytest.raises(subscriptions.ItemNotPending):
        subscriptions.undo_reject(conn, item_id, now=clock.now())
    subscriptions.approve(conn, item_id, now=clock.now())  # and it can be approved after the undo


def test_bulk_approve_and_reject(conn, lister, clock, sub):
    ids = [make_item(conn, lister, clock, sub, f"b{i}") for i in range(4)]
    subscriptions.reject(conn, ids[3], now=clock.now())
    approved = subscriptions.bulk_approve(conn, [ids[0], ids[1], ids[0], ids[3], 999], now=clock.now())
    assert [a[0] for a in approved.approved] == [ids[0], ids[1]] and approved.skipped == [ids[3], 999]
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2
    assert subscriptions.bulk_reject(conn, [ids[2], ids[0], 999], now=clock.now()) == 1  # ids[0] is approved
    assert subscriptions.list_inbox(conn) == []


def test_list_inbox_is_newest_first_with_a_channel_filter(conn, lister, clock, sub):
    lister.add_channel("UCother", "Other", videos=["x0"])
    other = subscriptions.subscribe(conn, lister, "https://www.youtube.com/channel/UCother", now=clock.now())
    for vid, day in (("a", 1), ("b", 3), ("c", 2)):
        lister.upload(CHANNEL, vid)
        lister.set_status(vid, published_at=datetime(2026, 9, day, tzinfo=UTC))
    lister.upload("UCother", "o")
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    subscriptions.check(conn, lister, other.id, now=clock.now())
    assert pending_ids(conn) == ["b", "c", "o", "a"]  # by publish date, whichever channel
    assert [i.youtube_id for i in subscriptions.list_inbox(conn, subscription_id=sub.id)] == ["b", "c", "a"]
    assert [i.youtube_id for i in subscriptions.list_inbox(conn, subscription_id=other.id)] == ["o"]


# --- pause, resume, remove (CS-4) -------------------------------------------------------


def test_pause_and_resume_catch_uploads_made_while_paused(conn, lister, clock, sub):
    subscriptions.pause(conn, sub.id)
    assert subscriptions.get_subscription(conn, sub.id).paused
    lister.upload(CHANNEL, "during1")
    lister.upload(CHANNEL, "during2")
    clock.advance(days=2)  # the paused subscription is not checked (the worker skips it)
    subscriptions.resume(conn, sub.id)
    assert not subscriptions.get_subscription(conn, sub.id).paused
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert set(pending_ids(conn)) == {"during1", "during2"} and len(result.new_item_ids) == 2


def test_a_paused_subscriptions_inbox_stays_actionable_and_counted(conn, lister, clock, sub):
    item_id = make_item(conn, lister, clock, sub)
    subscriptions.pause(conn, sub.id)
    assert subscriptions.inbox_counts(conn, clock.now())["pending"] == 1
    subscriptions.approve(conn, item_id, now=clock.now())


def test_unknown_subscription(conn, clock):
    for fn in (subscriptions.pause, subscriptions.resume, subscriptions.remove):
        with pytest.raises(KeyError):
            fn(conn, 42)
    with pytest.raises(KeyError):
        subscriptions.check(conn, FakeChannelLister(), 42, now=clock.now())


def test_remove_deletes_pending_keeps_rejections_and_the_library(conn, lister, clock, sub):
    pend, rej, appr = (make_item(conn, lister, clock, sub, v) for v in ("p", "r", "a"))
    subscriptions.reject(conn, rej, now=clock.now())
    source_id, _ = subscriptions.approve(conn, appr, now=clock.now())
    subscriptions.remove(conn, sub.id)
    assert subscriptions.get_subscription(conn, sub.id) is None and seen_count(conn, sub.id) == 0
    assert subscriptions.get_item(conn, pend) is None
    assert subscriptions.get_item(conn, rej).status == "rejected" and subscriptions.get_item(conn, rej).subscription_id is None
    assert subscriptions.get_item(conn, appr).status == "approved"
    assert ingest.get_source_video(conn, source_id) is not None


def test_removing_keeps_the_show_and_its_episodes(conn, lister, clock):
    show_id = library.create_show(conn, "Kids Fun", now=clock.now(), youtube_channel_id=CHANNEL)
    library.add_episode(conn, show_id, "Ep", "shows/1/e.mp4", now=clock.now())
    sub = subscriptions.subscribe(conn, lister, URL, show_id=show_id, now=clock.now())
    subscriptions.remove(conn, sub.id)
    assert library.get_show(conn, show_id) is not None and len(library.list_episodes(conn, show_id)) == 1


def test_resubscribing_takes_a_fresh_baseline_and_keeps_rejections(conn, lister, clock, sub):
    rejected = make_item(conn, lister, clock, sub, "nope")
    subscriptions.reject(conn, rejected, now=clock.now())
    lister.upload(CHANNEL, "while-gone")  # uploaded before re-subscribing: part of the new baseline
    subscriptions.remove(conn, sub.id)
    clock.advance(days=3)
    again = subscriptions.subscribe(conn, lister, URL, now=clock.now())
    assert again.baseline_at == clock.now() and again.created_at > sub.created_at
    assert subscriptions.check(conn, lister, again.id, now=clock.now()).new_item_ids == []
    assert subscriptions.get_item(conn, rejected).status == "rejected"
    lister.upload(CHANNEL, "nope2")
    lister.channels[CHANNEL]["videos"].insert(0, entry("nope"))  # the rejected one shows up again
    subscriptions.check(conn, lister, again.id, now=clock.now())
    assert pending_ids(conn) == ["nope2"]  # a rejected id never comes back (A-34)


# --- counts and schedule (HA-9, CS-2) -------------------------------------------------------


def test_inbox_counts_follow_arrivals_and_decisions(conn, lister, clock, sub):
    assert subscriptions.inbox_counts(conn, clock.now()) == {"pending": 0, "unhealthy": 0, "latest_received_at": None}
    first = make_item(conn, lister, clock, sub, "c1")
    clock.advance(hours=1)
    second = make_item(conn, lister, clock, sub, "c2")
    counts = subscriptions.inbox_counts(conn, clock.now())
    assert counts == {"pending": 2, "unhealthy": 0, "latest_received_at": to_db(clock.now())}
    subscriptions.reject(conn, second, now=clock.now())
    subscriptions.approve(conn, first, now=clock.now())
    counts = subscriptions.inbox_counts(conn, clock.now())
    assert counts["pending"] == 0 and counts["latest_received_at"] == to_db(clock.now())  # decided rows still count


def test_check_interval_defaults_to_six_hours_and_has_a_one_hour_minimum(conn):
    assert subscriptions.get_check_hours(conn) == 6
    subscriptions.set_check_hours(conn, 1)
    assert subscriptions.get_check_hours(conn) == 1
    for bad in (0, -3):
        with pytest.raises(ValueError):
            subscriptions.set_check_hours(conn, bad)
    assert subscriptions.get_check_hours(conn) == 1


def test_a_new_subscription_is_due_at_once_and_again_after_the_interval(conn, lister, clock, sub):
    assert subscriptions.due_subscriptions(conn, clock.now()) == [sub.id]
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert subscriptions.due_subscriptions(conn, clock.now()) == []
    clock.advance(hours=5)
    assert subscriptions.due_subscriptions(conn, clock.now()) == []
    clock.advance(hours=1)
    assert subscriptions.due_subscriptions(conn, clock.now()) == [sub.id]


def test_a_paused_subscription_is_not_due_unless_checked_now(conn, lister, clock, sub):
    subscriptions.pause(conn, sub.id)
    assert subscriptions.due_subscriptions(conn, clock.now()) == []
    assert subscriptions.request_check(conn, None, now=clock.now()) == 0  # "check all" skips paused ones
    assert subscriptions.request_check(conn, sub.id, now=clock.now()) == 1
    assert subscriptions.due_subscriptions(conn, clock.now()) == [sub.id]
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert subscriptions.due_subscriptions(conn, clock.now()) == []  # the request is cleared by the check


def test_a_failed_check_also_clears_the_request(conn, lister, clock, sub):
    subscriptions.check(conn, lister, sub.id, now=clock.now())
    subscriptions.request_check(conn, sub.id, now=clock.now())
    lister.fail = RuntimeError("down")
    result = subscriptions.check(conn, lister, sub.id, now=clock.now())
    assert not result.ok
    assert subscriptions.due_subscriptions(conn, clock.now()) == []  # retried on the schedule, not in a loop


def test_request_check_for_an_unknown_subscription_raises(conn, clock):
    with pytest.raises(KeyError):
        subscriptions.request_check(conn, 99, now=clock.now())
