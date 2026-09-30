from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tellybox import ingest, jobs
from tellybox.clock import FakeClock
from tellybox.db import open_db
from tellybox.ingest import JobRunner
from tellybox.jobs import JobType
from tellybox.worker import Worker, update_due

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "t.db")


def local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).astimezone(UTC)


def test_update_due_when_never_run(conn):  # CI-5
    # Recording the fallback version at startup doesn't count as an update.
    ingest.record_tool_version(conn, "yt-dlp", "2026.08.19", None, now=local(2026, 9, 28, 11))
    assert update_due(conn, local(2026, 9, 28, 12), TZ)


@pytest.mark.parametrize(
    ("checked", "now", "due"),
    [
        (local(2026, 9, 28, 3, 5), local(2026, 9, 28, 12), False),   # checked after today's 03:00
        (local(2026, 9, 27, 12), local(2026, 9, 28, 2, 59), False),  # before today's slot; yesterday's done
        (local(2026, 9, 27, 12), local(2026, 9, 28, 3, 0), True),    # today's slot reached
        (local(2026, 9, 26, 12), local(2026, 9, 28, 1), True),       # missed a day
    ],
)
def test_update_due_daily_at_three(conn, checked, now, due):
    job_id = ingest.request_ytdlp_update(conn, now=checked)
    jobs.complete(conn, job_id, now=checked)
    assert update_due(conn, now, TZ) is due


class RecordingRunner(JobRunner):
    def __init__(self, conn, clock):
        super().__init__(conn=conn, media_dir=None, tools_dir=None, ytdlp=None, clock=clock)
        self.ran = []

    def run(self, job):
        self.ran.append(job.id)
        jobs.complete(self.conn, job.id, now=self.clock.now())


def test_step_runs_one_job_and_queues_first_update(conn):
    clock = FakeClock(local(2026, 9, 28, 12))
    runner = RecordingRunner(conn, clock)
    worker = Worker(runner, TZ)
    _, job_id = ingest.add(conn, _info(), publish=True, now=clock.now())
    assert worker.step()  # never checked -> update queued, but the download was first in line
    assert runner.ran == [job_id]
    assert jobs.has_pending(conn, JobType.UPDATE_YTDLP)
    assert worker.step()
    assert not worker.step()


def test_no_auto_update_when_disabled(conn):
    clock = FakeClock(local(2026, 9, 28, 12))
    worker = Worker(RecordingRunner(conn, clock), TZ, auto_update=False)
    assert not worker.step()
    assert not jobs.has_pending(conn, JobType.UPDATE_YTDLP)


def _info():
    from tellybox.ytdlp import VideoInfo
    return VideoInfo(youtube_id="w1", url="https://www.youtube.com/watch?v=w1", title="t", channel_id="c",
                     channel_name="C", duration_s=1.0, thumbnail_url=None, chapters=[], is_live=False)


# --------------------------------------------------------------------------- purge (AD-5)


def _old_watch_session(conn, now):
    from tellybox.db import to_db
    conn.execute(
        "INSERT INTO watch_session (id, episode_id, started_at, ended_at, seconds_counted) VALUES (1, NULL, ?, ?, 0)",
        (to_db(now - timedelta(days=40)), to_db(now - timedelta(days=22))),
    )


def test_purge_runs_once_in_the_daily_update_slot_and_again_the_next(conn):
    clock = FakeClock(local(2026, 9, 28, 12))  # well after today's 03:00 slot
    _old_watch_session(conn, clock.now())
    worker = Worker(RecordingRunner(conn, clock), TZ)

    assert worker.step()  # never checked before: update queued and run, purge runs in the same slot
    assert conn.execute("SELECT count(*) FROM watch_session").fetchone()[0] == 0

    # Later the same day: the update check (and so the purge) isn't due again.
    _old_watch_session(conn, clock.now())
    clock.advance(hours=1)
    assert not worker.step()
    assert conn.execute("SELECT count(*) FROM watch_session").fetchone()[0] == 1

    # The next day's 03:00 slot: due again.
    clock.advance(days=1)
    assert worker.step()
    assert conn.execute("SELECT count(*) FROM watch_session").fetchone()[0] == 0


def test_no_auto_purge_when_disabled(conn):
    clock = FakeClock(local(2026, 9, 28, 12))
    _old_watch_session(conn, clock.now())
    worker = Worker(RecordingRunner(conn, clock), TZ, auto_purge=False)
    worker.step()
    assert conn.execute("SELECT count(*) FROM watch_session").fetchone()[0] == 1


def test_purge_runs_daily_at_03_00_without_the_ytdlp_update(conn):
    clock = FakeClock(local(2026, 9, 28, 12))
    worker = Worker(RecordingRunner(conn, clock), TZ, auto_update=False)
    _old_watch_session(conn, clock.now())
    worker.step()  # at startup
    assert conn.execute("SELECT count(*) FROM watch_session").fetchone()[0] == 0

    _old_watch_session(conn, clock.now())
    clock.set(local(2026, 9, 29, 2, 59))
    worker.step()  # still the same 03:00-to-03:00 day
    assert conn.execute("SELECT count(*) FROM watch_session").fetchone()[0] == 1
    clock.set(local(2026, 9, 29, 3, 0))
    worker.step()
    assert conn.execute("SELECT count(*) FROM watch_session").fetchone()[0] == 0


# --------------------------------------------------------------------------- SponsorBlock re-checks (SB-3)


def _source(conn, youtube_id, now, *, status="ready", until=None, sb_status=None):
    from tellybox.db import to_db
    cur = conn.execute(
        """INSERT INTO source_video (youtube_id, url, title, publish, status, sb_status, sb_recheck_until, created_at, updated_at)
           VALUES (?, 'u', 't', 'publish', ?, ?, ?, ?, ?)""",
        (youtube_id, status, sb_status, to_db(until if until is not None else now + timedelta(days=3)),
         to_db(now), to_db(now)),
    )
    return cur.lastrowid


def _rechecks(conn):
    return [r["target_id"] for r in conn.execute("SELECT target_id FROM job WHERE type = 'sb_recheck' ORDER BY id")]


def test_rechecks_are_queued_once_per_day_for_videos_in_their_window(conn):
    clock = FakeClock(local(2026, 9, 28, 12))
    now = clock.now()
    due = _source(conn, "a", now)
    _source(conn, "b", now, until=now - timedelta(hours=1))  # window passed
    _source(conn, "c", now, sb_status="admin_off")
    _source(conn, "d", now, status="failed")
    busy = _source(conn, "e", now)
    jobs.enqueue(conn, JobType.REDOWNLOAD, busy, now=now)
    worker = Worker(RecordingRunner(conn, clock), TZ, auto_update=False, auto_purge=False)

    worker.step()  # at startup
    assert _rechecks(conn) == [due]
    assert conn.execute("SELECT max_attempts FROM job WHERE type = 'sb_recheck'").fetchone()[0] == 2

    while worker.step():
        pass
    clock.advance(hours=2)  # the same slot day: not again
    worker.step()
    assert _rechecks(conn) == [due]

    clock.advance(days=1)  # the next 03:00 slot; the redownload of "e" has finished by now
    worker.step()
    assert _rechecks(conn) == [due, due, busy]


def test_a_video_with_a_pending_recheck_is_not_queued_again(conn):
    clock = FakeClock(local(2026, 9, 28, 12))
    sid = _source(conn, "a", clock.now())
    jobs.enqueue(conn, JobType.SB_RECHECK, sid, now=clock.now(), max_attempts=2)
    Worker(RecordingRunner(conn, clock), TZ, auto_update=False, auto_purge=False)._enqueue_rechecks(clock.now())
    assert _rechecks(conn) == [sid]


def test_no_auto_recheck_when_disabled(conn):
    clock = FakeClock(local(2026, 9, 28, 12))
    _source(conn, "a", clock.now())
    Worker(RecordingRunner(conn, clock), TZ, auto_update=False, auto_purge=False, auto_recheck=False).step()
    assert _rechecks(conn) == []


def test_cast_now_playing_reads_the_episode_from_the_cast_state(monkeypatch):  # SB-3
    import httpx

    from tellybox.worker.__main__ import cast_now_playing

    state = {"now_playing": {"episode_id": 7, "state": "PAUSED"}}
    urls = []

    def get(url, timeout):
        urls.append(url)
        return httpx.Response(200, json=state, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    now_playing = cast_now_playing("127.0.0.1", 8081)
    assert now_playing() == 7 and urls == ["http://127.0.0.1:8081/state"]
    state["now_playing"] = None
    assert now_playing() is None


def test_cast_now_playing_raises_when_the_cast_service_is_down(monkeypatch):
    import httpx

    from tellybox.worker.__main__ import cast_now_playing

    def get(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(httpx.ConnectError):
        cast_now_playing("127.0.0.1", 8081)()
