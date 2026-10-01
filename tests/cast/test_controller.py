"""Cast controller behaviour with a fake clock and a fake Chromecast (no real device)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tellybox import library
from tellybox.cast.controller import CastController, EndReason, PlayRefused
from tellybox.cast.device import MediaStatus, PlayerState
from tellybox.cast.fake import FakeCastDevice
from tellybox.clock import FakeClock
from tellybox.db import open_db

TZ = ZoneInfo("Europe/Amsterdam")
START = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)  # 16:00 local
EPISODE_S = 600.0


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "tellybox.db")


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def fake(clock):
    return FakeCastDevice(clock, default_duration_s=EPISODE_S)


@pytest.fixture
def episodes(conn, clock):
    show_id = library.create_show(conn, "Dev show", now=clock.now())
    return [
        library.add_episode(conn, show_id, f"Episode {i}", f"dev/ep{i}.mp4", now=clock.now(), duration_s=EPISODE_S)
        for i in (1, 2, 3)
    ]


def configure(conn, *, allowance_min=60, mode="ignore_pauses", max_session_min=90, grace_min=15):
    conn.execute(
        "UPDATE profile SET daily_allowance_min = ?, counting_mode = ?, max_session_min = ?",
        (allowance_min, mode, max_session_min),
    )
    conn.execute("UPDATE settings SET grace_cap_min = ?", (grace_min,))


async def make_controller(conn, clock, fake) -> CastController:
    async def sleep(seconds: float) -> None:  # the controller's pauses move the fake clock
        clock.advance(seconds)

    ctrl = CastController(conn, clock=clock, tz=TZ, media_base_url="http://tv.test:8080", secret=b"test-secret",
                          device=fake, sleep=sleep)
    await ctrl.start(run_loops=False)
    await pump(ctrl, fake)
    return ctrl


async def pump(ctrl, fake):
    while events := fake.drain():
        for event in events:
            await ctrl.handle_event(event)


async def run_for(ctrl, fake, clock, seconds: float, *, auto_finish=True):
    """Simulate the 1 s tick loop; the fake episode ends when its duration is reached."""
    for _ in range(int(seconds)):
        clock.advance(1)
        if auto_finish and ctrl.current and ctrl.current.player_state == PlayerState.PLAYING:
            if fake.position() >= (ctrl.current.duration_s or EPISODE_S):
                fake.finish()
        await pump(ctrl, fake)
        await ctrl.tick()
        await pump(ctrl, fake)


def used_s(conn) -> float:
    row = conn.execute("SELECT seconds_used FROM daily_usage").fetchone()
    return row[0] if row else 0.0


def sessions(conn):
    return conn.execute("SELECT * FROM watch_session ORDER BY id").fetchall()


def play_calls(fake):
    return [c for c in fake.calls if c[0] == "play"]


async def play(ctrl, fake, episode_id):
    await ctrl.play(episode_id)
    await pump(ctrl, fake)


# --------------------------------------------------------------------------- playing and counting


async def test_pick_casts_signed_url_and_counts_playing_time(conn, clock, fake, episodes):  # KA-5, PB-2, WT-2
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])

    (_, url, start_s), = play_calls(fake)
    assert f"/media/{episodes[0]}/" in url and url.startswith("http://tv.test:8080/")
    assert start_s == 0
    assert ctrl.state()["now_playing"]["state"] == "playing"

    await run_for(ctrl, fake, clock, 60)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(60, abs=1)


async def test_usage_persisted_at_least_every_30_seconds(conn, clock, fake, episodes):  # WT-8
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    assert used_s(conn) >= 15
    assert sessions(conn)[0]["seconds_counted"] >= 15


async def test_pauses_not_counted_in_ignore_pauses_mode(conn, clock, fake, episodes):  # WT-2
    configure(conn, mode="ignore_pauses")
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    await ctrl.pause()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 60)
    assert ctrl.state()["now_playing"]["state"] == "paused"
    await ctrl.resume()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 30)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(60, abs=1)


async def test_pauses_counted_in_wall_clock_mode(conn, clock, fake, episodes):  # WT-2
    configure(conn, mode="wall_clock")
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    await ctrl.pause()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 60)
    await ctrl.resume()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 30)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(120, abs=1)


async def test_pause_from_another_controller_is_seen(conn, clock, fake, episodes):  # spike item 4
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 10)
    fake.external_pause()
    await pump(ctrl, fake)
    assert ctrl.state()["now_playing"]["state"] == "paused"
    await run_for(ctrl, fake, clock, 60)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(10, abs=1)


async def test_buffering_counts_as_playing(conn, clock, fake, episodes):  # owner decision
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    fake.buffer()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 20)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(20, abs=1)


async def test_new_pick_replaces_current_episode(conn, clock, fake, episodes):  # KA-5, A-5
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    await play(ctrl, fake, episodes[1])

    first, second = sessions(conn)
    assert first["end_reason"] == EndReason.REPLACED and first["ended_at"] is not None
    assert first["seconds_counted"] == pytest.approx(30, abs=1)
    assert second["episode_id"] == episodes[1] and second["ended_at"] is None
    assert ctrl.current.episode.id == episodes[1]


async def test_replacing_pick_keeps_counting_the_new_episode(conn, clock, fake, episodes):  # KA-5, WT-2, WT-9
    """The replaced media's INTERRUPTED status arrives with the new URL; it must not end the new session."""
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    await play(ctrl, fake, episodes[1])
    await run_for(ctrl, fake, clock, 40)
    ctrl.persist(clock.now())

    assert ctrl.current is not None and ctrl.current.episode.id == episodes[1]
    second = sessions(conn)[1]
    assert second["ended_at"] is None
    assert used_s(conn) == pytest.approx(70, abs=1)


async def test_repicking_the_same_episode_in_the_same_second_keeps_it(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await play(ctrl, fake, episodes[0])  # same second: the very same signed URL
    assert play_calls(fake)[0][1] == play_calls(fake)[1][1]
    await run_for(ctrl, fake, clock, 20)
    assert ctrl.current is not None
    assert [s["end_reason"] for s in sessions(conn)] == [EndReason.REPLACED, None]


async def test_late_finished_of_the_replaced_media_is_ignored(conn, clock, fake, episodes):  # PB-3
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    old_session = fake.media.media_session_id
    await play(ctrl, fake, episodes[1])
    url = ctrl.current.url
    await ctrl.handle_event(MediaStatus(PlayerState.IDLE, url, 0.0, None, "FINISHED", old_session))
    assert ctrl.current is not None and ctrl.current.episode.id == episodes[1]
    assert len(play_calls(fake)) == 2  # no autoplay


async def test_rewatching_counts(conn, clock, fake, episodes):  # WT-6
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(60, abs=1)


# --------------------------------------------------------------------------- autoplay and positions


async def test_autoplay_next_episode(conn, clock, fake, episodes):  # PB-3
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, EPISODE_S + 5)

    assert [f"/media/{e}/" in c[1] for c, e in zip(play_calls(fake), episodes)] == [True, True]
    first = sessions(conn)[0]
    assert first["end_reason"] == EndReason.FINISHED
    pos = conn.execute("SELECT finished FROM playback_position WHERE episode_id = ?", (episodes[0],)).fetchone()
    assert pos["finished"] == 1
    assert ctrl.current.episode.id == episodes[1]


async def test_no_autoplay_when_show_has_it_off(conn, clock, fake, episodes):  # PB-3, LM-4
    conn.execute("UPDATE show SET autoplay = 0")
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, EPISODE_S + 5)
    assert len(play_calls(fake)) == 1
    assert fake.calls[-1] == ("stop",)
    assert ctrl.current is None


async def test_end_of_show_stops(conn, clock, fake, episodes):  # PB-3
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[2])
    await run_for(ctrl, fake, clock, EPISODE_S + 5)
    assert len(play_calls(fake)) == 1
    assert ctrl.current is None


async def test_resume_where_left_off(conn, clock, fake, episodes):  # PB-4
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 100)
    await ctrl.stop()
    await pump(ctrl, fake)
    await play(ctrl, fake, episodes[0])
    assert play_calls(fake)[-1][2] == pytest.approx(100, abs=2)


async def test_episode_counts_as_finished_at_95_percent(conn, clock, fake, episodes):  # PB-4
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 0.96 * EPISODE_S)
    await ctrl.stop()
    await pump(ctrl, fake)
    row = conn.execute("SELECT finished FROM playback_position WHERE episode_id = ?", (episodes[0],)).fetchone()
    assert row["finished"] == 1
    await play(ctrl, fake, episodes[0])
    assert play_calls(fake)[-1][2] == 0


# --------------------------------------------------------------------------- time up


async def test_allowance_runs_out_episode_finishes_then_stops(conn, clock, fake, episodes):  # WT-4, KA-9
    configure(conn, allowance_min=5)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 310)
    state = ctrl.state()
    assert state["now_playing"]["state"] == "playing"
    assert state["timer"]["action"] == "finish_then_stop" and state["time_up"]

    await run_for(ctrl, fake, clock, EPISODE_S)
    assert len(play_calls(fake)) == 1  # no autoplay
    assert ctrl.current is None and fake.calls[-1] == ("stop",)
    assert sessions(conn)[0]["end_reason"] == EndReason.FINISHED
    with pytest.raises(PlayRefused):
        await ctrl.play(episodes[1])


async def test_grace_is_capped(conn, clock, fake, episodes):  # WT-5
    configure(conn, allowance_min=1, grace_min=15)
    fake.durations[f"/media/{episodes[0]}/"] = 3 * 3600
    conn.execute("UPDATE episode SET duration_s = ? WHERE id = ?", (3 * 3600, episodes[0]))
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 60 + 15 * 60 - 2)
    assert ctrl.current is not None
    await run_for(ctrl, fake, clock, 3)
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.TIME_UP
    assert fake.calls[-1] == ("stop",)


async def test_max_session_length_ends_viewing(conn, clock, fake, episodes):  # WT-3
    configure(conn, allowance_min=600, max_session_min=20, grace_min=15)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20 * 60 + 5)  # episode 2 is playing when the maximum is reached
    assert ctrl.state()["timer"]["reason"] == "session_max"
    await run_for(ctrl, fake, clock, 10 * 60)  # episode 2 finishes; no autoplay
    assert ctrl.current is None
    assert len(play_calls(fake)) == 2
    with pytest.raises(PlayRefused):
        await ctrl.play(episodes[2])


# --------------------------------------------------------------------------- overrides (WT-7)


async def test_block_stops_immediately(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 10)
    await ctrl.override("block")
    await pump(ctrl, fake)
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.BLOCKED
    with pytest.raises(PlayRefused):
        await ctrl.play(episodes[0])
    assert conn.execute("SELECT kind FROM override_log").fetchone()["kind"] == "block"


async def test_stop_now(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await ctrl.override("stop_now")
    await pump(ctrl, fake)
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.PARENT_STOP
    await play(ctrl, fake, episodes[0])  # stop now doesn't block further viewing
    assert ctrl.current is not None


async def test_extra_minutes_after_time_up(conn, clock, fake, episodes):
    configure(conn, allowance_min=1)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await ctrl.stop()
    await pump(ctrl, fake)
    clock.advance(0)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 70)
    await ctrl.stop()
    with pytest.raises(PlayRefused):
        await ctrl.play(episodes[1])
    await ctrl.override("extra_minutes", 10)
    await play(ctrl, fake, episodes[1])
    assert ctrl.current.episode.id == episodes[1]


async def test_unlimited_lifts_time_up(conn, clock, fake, episodes):
    configure(conn, allowance_min=1)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 70)
    assert ctrl.state()["timer"]["action"] == "finish_then_stop"
    await ctrl.override("unlimited")
    assert ctrl.state()["timer"]["action"] == "continue"
    assert ctrl.state()["timer"]["remaining_s"] is None


# --------------------------------------------------------------------------- other casts, disconnects (WT-9, PB-5)


async def test_foreign_cast_ends_session_and_is_not_counted(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    fake.takeover()
    await pump(ctrl, fake)
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER
    await run_for(ctrl, fake, clock, 120)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(30, abs=1)
    assert ("stop",) not in fake.calls  # never control other casts


async def test_foreign_status_ignored_when_idle(conn, clock, fake, episodes):  # WT-9
    ctrl = await make_controller(conn, clock, fake)
    fake.takeover()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 60)
    ctrl.persist(clock.now())
    assert used_s(conn) == 0
    assert fake.calls == [("connect",)]


async def test_stop_from_another_controller(conn, clock, fake, episodes):  # PB-5
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    url = ctrl.current.url
    await ctrl.handle_event(MediaStatus(PlayerState.IDLE, url, 0.0, EPISODE_S, idle_reason="CANCELLED"))
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.STOPPED


async def test_stop_from_another_controller_after_a_replace(conn, clock, fake, episodes):  # PB-5
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await play(ctrl, fake, episodes[1])
    ours = fake.media.media_session_id
    await ctrl.handle_event(MediaStatus(PlayerState.IDLE, ctrl.current.url, 0.0, EPISODE_S, "CANCELLED", ours))
    assert ctrl.current is None
    assert sessions(conn)[-1]["end_reason"] == EndReason.STOPPED


async def test_short_connection_loss_keeps_session(conn, clock, fake, episodes):  # PB-5
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    fake.lose_connection()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 20)
    fake.reconnect()
    await pump(ctrl, fake)
    assert ctrl.current is not None
    assert ctrl.state()["connection"] == "CONNECTED"
    await run_for(ctrl, fake, clock, 10)
    ctrl.persist(clock.now())
    assert used_s(conn) == pytest.approx(40, abs=2)  # the outage isn't counted


async def test_power_cycle_ends_session_as_disconnected(conn, clock, fake, episodes):  # PB-5
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    fake.power_cycle()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 40)
    fake.reconnect()
    await pump(ctrl, fake)
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.DISCONNECTED
    assert ctrl.state()["now_playing"] is None


async def test_long_connection_loss_ends_session(conn, clock, fake, episodes):  # PB-5
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    fake.lose_connection()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 301)
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.DISCONNECTED


# --------------------------------------------------------------------------- restart recovery (NF-7)


async def test_restart_reattaches_to_our_playing_media(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 40)
    url, session_id = ctrl.current.url, ctrl.current.watch_session_id
    # Process dies without a clean shutdown; the TV keeps playing.
    clock.advance(20)
    fresh = FakeCastDevice(clock, default_duration_s=EPISODE_S)
    fresh.preload(url, position_s=60)
    ctrl2 = await make_controller(conn, clock, fresh)

    assert ctrl2.current is not None
    assert ctrl2.current.watch_session_id == session_id
    assert ctrl2.state()["now_playing"]["position_s"] == 60
    before = used_s(conn)
    await run_for(ctrl2, fresh, clock, 30)
    ctrl2.persist(clock.now())
    assert used_s(conn) == pytest.approx(before + 30, abs=2)


async def test_restart_with_nothing_playing_closes_open_session(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 40)
    heartbeat = sessions(conn)[0]["last_heartbeat_at"]
    clock.advance(20)
    ctrl2 = await make_controller(conn, clock, FakeCastDevice(clock))
    assert ctrl2.current is None
    await run_for(ctrl2, ctrl2.device, clock, 11)
    row = sessions(conn)[0]
    assert row["end_reason"] == EndReason.RESTART
    assert row["ended_at"] == heartbeat


async def test_timer_state_survives_restart(conn, clock, fake, episodes):  # WT-8
    configure(conn, allowance_min=1)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 70)
    await ctrl.stop()
    ctrl2 = await make_controller(conn, clock, FakeCastDevice(clock))
    assert ctrl2.state()["time_up"]
    with pytest.raises(PlayRefused):
        await ctrl2.play(episodes[1])


# --------------------------------------------------------------------------- live state (KA-7)


async def test_subscribers_get_state_changes(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    queue = ctrl.subscribe()
    assert queue.get_nowait()["now_playing"] is None
    await play(ctrl, fake, episodes[0])
    snapshots = []
    while not queue.empty():
        snapshots.append(queue.get_nowait())
    assert snapshots[-1]["now_playing"]["state"] == "playing"
    ctrl.unsubscribe(queue)
