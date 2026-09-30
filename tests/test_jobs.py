"""SQLite-backed job queue (CI-3, NF-7, NF-8)."""

from datetime import timedelta

import pytest

from tellybox import db, jobs
from tellybox.clock import FakeClock
from tellybox.jobs import JobStatus, JobType


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.db")
    yield c
    c.close()


def test_enqueue_defaults(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 7, now=clock.now())
    job = jobs.get(conn, jid)
    assert job == jobs.Job(
        id=jid, type=JobType.DOWNLOAD, target_id=7, status=JobStatus.QUEUED, progress=None, error=None,
        attempts=0, max_attempts=4, run_after=clock.now(), heartbeat_at=None,
        created_at=clock.now(), updated_at=clock.now(), finished_at=None,
    )
    assert isinstance(job.type, JobType) and isinstance(job.status, JobStatus)
    assert jobs.get(conn, 999) is None


def test_claim_sets_running_status_per_type(conn, clock):
    d = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    u = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=clock.now())
    clock.advance(5)
    job = jobs.claim_next(conn, now=clock.now())
    assert (job.id, job.status, job.attempts, job.progress) == (d, JobStatus.DOWNLOADING, 1, 0.0)
    assert job.heartbeat_at == clock.now() and job.updated_at == clock.now()
    job = jobs.claim_next(conn, now=clock.now())
    assert (job.id, job.status, job.target_id) == (u, JobStatus.PROCESSING, None)
    assert jobs.claim_next(conn, now=clock.now()) is None


def test_claim_order_and_run_after(conn, clock):
    t0 = clock.now()
    a = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=t0)
    b = jobs.enqueue(conn, JobType.DOWNLOAD, 2, now=t0)
    # c was enqueued earlier (older run_after), so it goes first despite the higher id.
    c = jobs.enqueue(conn, JobType.DOWNLOAD, 3, now=t0 - timedelta(seconds=10))
    assert [jobs.claim_next(conn, now=t0).id for _ in range(3)] == [c, a, b]


def test_claim_respects_run_after(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    jobs.fail(conn, jid, "boom", now=clock.now(), retryable=True)
    clock.advance(59)
    assert jobs.claim_next(conn, now=clock.now()) is None
    clock.advance(1)
    assert jobs.claim_next(conn, now=clock.now()).id == jid


def test_claim_never_returns_a_job_twice_across_connections(tmp_path, clock):
    path = tmp_path / "t.db"
    c1, c2 = db.open_db(path), db.open_db(path)
    ids = [jobs.enqueue(c1, JobType.DOWNLOAD, i, now=clock.now()) for i in range(6)]
    claimed = []
    for i in range(10):
        job = jobs.claim_next(c1 if i % 2 else c2, now=clock.now())
        if job:
            claimed.append(job.id)
    assert sorted(claimed) == ids
    c1.close()
    c2.close()


def test_concurrent_claimers_split_the_queue(tmp_path, clock):
    import threading

    path = tmp_path / "t.db"
    setup = db.open_db(path)
    ids = [jobs.enqueue(setup, JobType.DOWNLOAD, i, now=clock.now()) for i in range(40)]
    results: list[list[int]] = [[], [], []]

    def work(out):
        c = db.open_db(path)
        while job := jobs.claim_next(c, now=clock.now()):
            out.append(job.id)
        c.close()

    threads = [threading.Thread(target=work, args=(r,)) for r in results]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed = [j for r in results for j in r]
    assert sorted(claimed) == ids  # every job exactly once
    setup.close()


def test_set_status_and_heartbeat(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    clock.advance(10)
    jobs.heartbeat(conn, jid, now=clock.now(), progress=0.5)
    job = jobs.get(conn, jid)
    assert (job.progress, job.heartbeat_at, job.updated_at) == (0.5, clock.now(), clock.now())
    clock.advance(10)
    jobs.heartbeat(conn, jid, now=clock.now())
    assert jobs.get(conn, jid).progress == 0.5  # None = unchanged
    assert jobs.get(conn, jid).heartbeat_at == clock.now()
    jobs.set_status(conn, jid, JobStatus.PROCESSING, now=clock.now())
    job = jobs.get(conn, jid)
    assert (job.status, job.progress, job.heartbeat_at) == (JobStatus.PROCESSING, 0.0, clock.now())
    jobs.set_status(conn, jid, JobStatus.PROCESSING, now=clock.now(), progress=None)
    assert jobs.get(conn, jid).progress is None


def test_complete_clears_error(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    jobs.fail(conn, jid, "flaky", now=clock.now(), retryable=True)
    clock.advance(60)
    job = jobs.claim_next(conn, now=clock.now())
    assert job.error == "flaky"  # kept visible until success
    clock.advance(30)
    jobs.complete(conn, jid, now=clock.now())
    job = jobs.get(conn, jid)
    assert (job.status, job.progress, job.error, job.finished_at) == (JobStatus.READY, 1.0, None, clock.now())
    assert job.attempts == 2


def test_backoff_sequence_then_gives_up(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    waits = []
    for attempt in range(1, 4):
        job = jobs.claim_next(conn, now=clock.now())
        assert job.attempts == attempt
        job = jobs.fail(conn, jid, f"err {attempt}", now=clock.now(), retryable=True)
        assert (job.status, job.error, job.finished_at) == (JobStatus.QUEUED, f"err {attempt}", None)
        waits.append((job.run_after - clock.now()).total_seconds())
        clock.set(job.run_after)
    assert waits == [60, 300, 1800]
    jobs.claim_next(conn, now=clock.now())
    job = jobs.fail(conn, jid, "err 4", now=clock.now(), retryable=True)
    assert (job.status, job.attempts, job.error, job.finished_at) == (JobStatus.FAILED, 4, "err 4", clock.now())


def test_backoff_caps_at_last_value(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now(), max_attempts=6)
    for _ in range(4):
        clock.set(jobs.claim_next(conn, now=clock.now() + timedelta(hours=1)).run_after)
        job = jobs.fail(conn, jid, "x", now=clock.now(), retryable=True)
    assert (job.run_after - clock.now()).total_seconds() == 1800


def test_non_retryable_fails_immediately(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    job = jobs.fail(conn, jid, "video unavailable", now=clock.now(), retryable=False)
    assert (job.status, job.attempts, job.finished_at) == (JobStatus.FAILED, 1, clock.now())


def test_admin_retry_only_from_failed(conn, clock):
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    with pytest.raises(ValueError):
        jobs.retry(conn, jid, now=clock.now())  # queued
    jobs.claim_next(conn, now=clock.now())
    with pytest.raises(ValueError):
        jobs.retry(conn, jid, now=clock.now())  # running
    jobs.fail(conn, jid, "nope", now=clock.now(), retryable=False)
    clock.advance(100)
    job = jobs.retry(conn, jid, now=clock.now())
    assert (job.status, job.attempts, job.run_after, job.finished_at) == (JobStatus.QUEUED, 0, clock.now(), None)
    assert job.error == "nope"
    assert jobs.claim_next(conn, now=clock.now()).id == jid
    jobs.complete(conn, jid, now=clock.now())
    with pytest.raises(ValueError):
        jobs.retry(conn, jid, now=clock.now())  # ready
    with pytest.raises(KeyError):
        jobs.retry(conn, 999, now=clock.now())


def test_recover_stale(conn, clock):
    t0 = clock.now()
    fresh = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=t0)
    stale = jobs.enqueue(conn, JobType.DOWNLOAD, 2, now=t0)
    no_hb = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=t0)
    queued = jobs.enqueue(conn, JobType.DOWNLOAD, 3, now=t0)
    for _ in range(3):
        jobs.claim_next(conn, now=t0)
    conn.execute("UPDATE job SET heartbeat_at = NULL WHERE id = ?", (no_hb,))
    clock.advance(jobs.STALE_AFTER_S - 1)
    jobs.heartbeat(conn, fresh, now=clock.now())
    [job] = jobs.recover_stale(conn, now=clock.now())
    assert job.id == no_hb
    clock.advance(1)
    recovered = jobs.recover_stale(conn, now=clock.now())
    assert [j.id for j in recovered] == [stale]
    job = jobs.get(conn, stale)
    assert recovered == [job]
    assert (job.status, job.run_after, job.attempts, job.progress) == (JobStatus.QUEUED, clock.now(), 1, None)
    assert jobs.get(conn, fresh).status == JobStatus.DOWNLOADING
    assert jobs.get(conn, queued).status == JobStatus.QUEUED
    assert jobs.STALE_AFTER_S == 60


def test_recover_stale_at_startup_takes_every_running_job(conn, clock):
    # NF-7: after a worker restart every running job is orphaned, even with a fresh heartbeat.
    a = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    b = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=clock.now())
    c = jobs.enqueue(conn, JobType.DOWNLOAD, 2, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    clock.advance(3)
    jobs.heartbeat(conn, a, now=clock.now())
    recovered = jobs.recover_stale(conn, now=clock.now(), stale_after_s=0)
    assert [j.id for j in recovered] == [a, b]
    assert all(j.status == JobStatus.QUEUED and j.run_after == clock.now() for j in recovered)
    assert jobs.get(conn, c).run_after == clock.now() - timedelta(seconds=3)  # untouched
    assert jobs.recover_stale(conn, now=clock.now(), stale_after_s=0) == []


def test_recover_stale_fails_job_out_of_attempts(conn, clock):
    # A job that keeps killing the worker must not loop forever (NF-7).
    jid = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now(), max_attempts=1)
    jobs.claim_next(conn, now=clock.now())
    clock.advance(120)
    [job] = jobs.recover_stale(conn, now=clock.now())
    assert (job.status, job.finished_at) == (JobStatus.FAILED, clock.now())
    assert job.error


def test_list_jobs(conn, clock):
    a = jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now())
    b = jobs.enqueue(conn, JobType.DOWNLOAD, 2, now=clock.now())
    c = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    assert [j.id for j in jobs.list_jobs(conn)] == [c, b, a]
    assert [j.id for j in jobs.list_jobs(conn, limit=2)] == [c, b]
    assert [j.id for j in jobs.list_jobs(conn, statuses=[JobStatus.QUEUED])] == [c, b]
    assert [j.id for j in jobs.list_jobs(conn, statuses=jobs.RUNNING)] == [a]
    assert jobs.list_jobs(conn, statuses=[]) == []


def test_has_pending_and_next_run_after(conn, clock):
    assert not jobs.has_pending(conn, JobType.UPDATE_YTDLP)
    assert jobs.next_run_after(conn) is None
    jid = jobs.enqueue(conn, JobType.UPDATE_YTDLP, None, now=clock.now())
    assert jobs.has_pending(conn, JobType.UPDATE_YTDLP)
    assert not jobs.has_pending(conn, JobType.DOWNLOAD)
    jobs.claim_next(conn, now=clock.now())
    assert jobs.has_pending(conn, JobType.UPDATE_YTDLP)  # running counts
    assert jobs.next_run_after(conn) is None  # only queued jobs
    job = jobs.fail(conn, jid, "x", now=clock.now(), retryable=True)
    assert jobs.next_run_after(conn) == job.run_after
    jobs.enqueue(conn, JobType.DOWNLOAD, 1, now=clock.now() + timedelta(seconds=5))
    assert jobs.next_run_after(conn) == clock.now() + timedelta(seconds=5)
    jobs.claim_next(conn, now=job.run_after)
    jobs.claim_next(conn, now=job.run_after)
    jobs.complete(conn, jid, now=clock.now())
    assert not jobs.has_pending(conn, JobType.UPDATE_YTDLP)


def test_new_job_types_claim_status(conn, clock):
    r = jobs.enqueue(conn, JobType.REDOWNLOAD, 3, now=clock.now())
    c = jobs.enqueue(conn, JobType.SB_RECHECK, 3, now=clock.now())
    assert jobs.claim_next(conn, now=clock.now()).status == JobStatus.DOWNLOADING
    assert jobs.claim_next(conn, now=clock.now()).status == JobStatus.PROCESSING
    assert jobs.get(conn, r).type == JobType.REDOWNLOAD and jobs.get(conn, c).type == JobType.SB_RECHECK


def test_defer_requeues_without_using_an_attempt(conn, clock):
    jid = jobs.enqueue(conn, JobType.REDOWNLOAD, 3, now=clock.now())
    jobs.claim_next(conn, now=clock.now())
    later = clock.now() + timedelta(minutes=20)
    jobs.defer(conn, jid, later, "episode is playing", now=clock.now())
    job = jobs.get(conn, jid)
    assert (job.status, job.attempts, job.run_after, job.error) == (JobStatus.QUEUED, 0, later, "episode is playing")
    assert jobs.claim_next(conn, now=clock.now()) is None


def test_has_pending_for(conn, clock):
    assert not jobs.has_pending_for(conn, 3, [JobType.SB_RECHECK, JobType.REDOWNLOAD])
    jid = jobs.enqueue(conn, JobType.REDOWNLOAD, 3, now=clock.now())
    assert jobs.has_pending_for(conn, 3, [JobType.SB_RECHECK, JobType.REDOWNLOAD])
    assert not jobs.has_pending_for(conn, 4, [JobType.REDOWNLOAD])
    assert not jobs.has_pending_for(conn, 3, [JobType.SB_RECHECK])
    jobs.claim_next(conn, now=clock.now())
    jobs.complete(conn, jid, now=clock.now())
    assert not jobs.has_pending_for(conn, 3, [JobType.REDOWNLOAD])
