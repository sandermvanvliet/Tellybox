"""The worker's periodic subscription check (CS-2, CS-4, A-35). Fake lister and clock; no network."""

from datetime import timedelta

import pytest

from tellybox import ingest, subscriptions
from tellybox.clock import FakeClock
from tellybox.db import open_db, to_db
from tellybox.worker import Worker
from tellybox.ytdlp import YtDlpError
from tests.channel_fakes import FakeChannelLister
from tests.test_worker import TZ, RecordingRunner, _info, local

CHANNEL = "UCkids"
URL = "https://www.youtube.com/@kidsfun"


@pytest.fixture
def conn():
    return open_db(":memory:")


@pytest.fixture
def clock():
    return FakeClock(local(2026, 9, 28, 12))


@pytest.fixture
def lister():
    fake = FakeChannelLister()
    fake.add_channel(CHANNEL, "Kids Fun", handle="@kidsfun", videos=[f"old{i:03d}" for i in range(5)])
    return fake


def make_worker(conn, lister, clock, **kw):
    return Worker(RecordingRunner(conn, clock), TZ, auto_update=False, auto_purge=False, auto_recheck=False,
                  lister=lister, **kw)


def subscribed_and_fresh(conn, lister, clock, url=URL):
    """Subscribed and just checked, so nothing is due."""
    sub = subscriptions.subscribe(conn, lister, url, now=clock.now())
    conn.execute("UPDATE subscription SET last_checked_at = ? WHERE id = ?", (to_db(clock.now()), sub.id))
    lister.calls.clear()
    return sub


def pending(conn):
    return [i.youtube_id for i in subscriptions.list_inbox(conn)]


def test_a_never_checked_subscription_is_checked_once(conn, lister, clock):
    sub = subscriptions.subscribe(conn, lister, URL, now=clock.now())
    lister.upload(CHANNEL, "new001")
    worker = make_worker(conn, lister, clock)
    worker.step()
    assert pending(conn) == ["new001"]
    assert subscriptions.get_subscription(conn, sub.id, now=clock.now()).last_checked_at is not None
    lister.calls.clear()
    worker.step()
    assert lister.calls == []  # a fresh check isn't repeated before the interval


def test_checked_again_after_the_interval(conn, lister, clock):
    subscribed_and_fresh(conn, lister, clock)
    worker = make_worker(conn, lister, clock)
    worker.step()
    assert lister.calls == []
    clock.advance(hours=subscriptions.get_check_hours(conn), seconds=1)
    lister.upload(CHANNEL, "new001")
    worker.step()
    assert pending(conn) == ["new001"]


def test_check_now_request_runs_it_early_and_is_cleared(conn, lister, clock):
    sub = subscribed_and_fresh(conn, lister, clock)
    lister.upload(CHANNEL, "new001")
    worker = make_worker(conn, lister, clock)
    worker.step()
    assert pending(conn) == []
    subscriptions.request_check(conn, sub.id, now=clock.now())
    worker.step()
    assert pending(conn) == ["new001"]
    lister.calls.clear()
    worker.step()
    assert lister.calls == []


def test_a_paused_subscription_is_only_checked_on_request(conn, lister, clock):
    sub = subscribed_and_fresh(conn, lister, clock)
    subscriptions.pause(conn, sub.id)
    lister.upload(CHANNEL, "new001")
    clock.advance(days=2)
    worker = make_worker(conn, lister, clock)
    worker.step()
    assert lister.calls == []
    subscriptions.request_check(conn, sub.id, now=clock.now())
    worker.step()
    assert pending(conn) == ["new001"]


def test_one_subscription_is_checked_per_step(conn, lister, clock):
    lister.add_channel("UCother", "Other", handle="@other", videos=["o1"])
    subscriptions.subscribe(conn, lister, URL, now=clock.now())
    subscriptions.subscribe(conn, lister, "https://www.youtube.com/@other", now=clock.now())
    lister.calls.clear()
    worker = make_worker(conn, lister, clock)
    worker.step()
    assert {c[0] for c in lister.calls} == {CHANNEL}
    worker.step()
    assert {c[0] for c in lister.calls} == {CHANNEL, "UCother"}


def test_a_failing_check_is_stored_and_the_job_still_runs(conn, lister, clock):
    sub = subscriptions.subscribe(conn, lister, URL, now=clock.now())
    lister.fail = YtDlpError("HTTP Error 404", retryable=False)
    _, job_id = ingest.add(conn, _info(), publish=True, now=clock.now())
    worker = make_worker(conn, lister, clock)
    assert worker.step()
    assert worker.runner.ran == [job_id]
    assert subscriptions.get_subscription(conn, sub.id, now=clock.now()).last_error


def test_an_unexpected_exception_is_logged_and_backs_off(conn, lister, clock, monkeypatch, caplog):
    subscriptions.subscribe(conn, lister, URL, now=clock.now())
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("bug")

    monkeypatch.setattr(subscriptions, "check", boom)
    _, job_id = ingest.add(conn, _info(), publish=True, now=clock.now())
    worker = make_worker(conn, lister, clock)
    assert worker.step()  # does not raise, and the job still ran
    assert worker.runner.ran == [job_id]
    assert "subscription check failed" in caplog.text
    worker.step()
    assert len(calls) == 1  # backed off, not retried on every poll
    clock.advance(minutes=6)
    worker.step()
    assert len(calls) == 2


def test_auto_check_false_does_nothing(conn, lister, clock):
    subscriptions.subscribe(conn, lister, URL, now=clock.now())
    lister.calls.clear()
    make_worker(conn, lister, clock, auto_check=False).step()
    assert lister.calls == []


def test_a_failure_listing_the_due_subscriptions_does_not_stop_the_loop(conn, lister, clock, monkeypatch):
    worker = make_worker(conn, lister, clock)

    def boom(conn, now):
        raise RuntimeError("db locked")

    monkeypatch.setattr(subscriptions, "due_subscriptions", boom)
    worker._check_one_subscription(clock.now())  # must not raise
