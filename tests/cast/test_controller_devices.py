"""Device (browser) sessions in the cast controller (PB-7..PB-9, WT-10..WT-12), with a fake clock and a fake Chromecast."""

import pytest

from tellybox import library, store
from tellybox.cast.controller import CastController, EndReason, PlayRefused, UnknownProfile
from tellybox.db import to_db
from tellybox.timer import TV

from test_controller import (  # noqa: F401  (fixtures)
    EPISODE_S,
    TZ,
    clock,
    configure,
    conn,
    episodes,
    fake,
    make_controller,
    pump,
    run_for,
    sessions,
)

DEV = "device-aaaa1111"
DEV2 = "device-bbbb2222"


@pytest.fixture
def kids(conn, clock):
    """Three profiles: 1 (the migrated household), 2 and 3."""
    for name in ("B", "C"):
        conn.execute("INSERT INTO profile (name, created_at) VALUES (?, ?)", (name, to_db(clock.now())))
    return [1, 2, 3]


def usage(conn) -> dict[int, float]:
    return {r["profile_id"]: r["seconds_used"] for r in conn.execute("SELECT * FROM daily_usage")}


def rows(conn, **where):
    return [r for r in sessions(conn) if all(r[k] == v for k, v in where.items())]


async def beat(ctrl, clock, device, state="playing", position=0.0, *, after=10.0, duration=EPISODE_S):
    """Move on ``after`` seconds (ticking once, as the service does), then send a heartbeat."""
    clock.advance(after)
    await ctrl.tick()
    return await ctrl.device_heartbeat(device, state, position, duration)


async def watch(ctrl, clock, device, seconds, position=0.0):
    """Heartbeats every 10 s while playing; returns the last answer."""
    answer = None
    for i in range(int(seconds // 10)):
        answer = await beat(ctrl, clock, device, "playing", position + 10 * (i + 1))
    return answer


# --------------------------------------------------------------------------- playing and counting


async def test_device_play_opens_a_device_session_and_counts_heartbeats(conn, clock, fake, episodes):  # PB-7, WT-10
    ctrl = await make_controller(conn, clock, fake)
    started = await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    assert started["start_s"] == 0
    wsid = sessions(conn)[0]["id"]
    assert started["url"].startswith(f"/media/{episodes[0]}/") and started["url"].endswith(f".mp4?s={wsid}")
    assert started["session"]["key"] == "device:" + DEV and started["session"]["state"] == "loading"
    row = sessions(conn)[0]
    assert (row["target"], row["device_label"], row["ended_at"]) == ("device", "iPhone Safari", None)

    await watch(ctrl, clock, DEV, 60)  # counting starts with the first "playing" heartbeat
    ctrl.persist(clock.now())
    assert usage(conn)[1] == pytest.approx(50, abs=1)
    assert sessions(conn)[0]["seconds_counted"] == pytest.approx(50, abs=1)
    assert ctrl.device_sessions[DEV].played_s == pytest.approx(50, abs=1)
    assert not fake.calls or all(c[0] in ("connect",) for c in fake.calls)  # the Chromecast was not involved


async def test_a_pause_is_not_counted_in_ignore_pauses_mode(conn, clock, fake, episodes):  # WT-2, WT-10
    configure(conn, mode="ignore_pauses")
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "Pixel Chrome", episodes[0], [1])
    await watch(ctrl, clock, DEV, 30)
    for _ in range(6):
        await beat(ctrl, clock, DEV, "paused", 30)
    ctrl.persist(clock.now())
    assert usage(conn)[1] == pytest.approx(30, abs=1)  # 20 s, plus the interval that ended in the pause report
    assert ctrl.state()["sessions"][0]["state"] == "paused"


async def test_a_heartbeat_credit_is_capped(conn, clock, fake, episodes):  # WT-10
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPad Safari", episodes[0], [1])
    await beat(ctrl, clock, DEV, "playing", 1)
    clock.advance(200)  # a long gap with no tick: the silence rule applies at +30 s
    await ctrl.device_heartbeat(DEV, "playing", 2, EPISODE_S)
    assert ctrl.device_sessions[DEV].played_s == 30
    ctrl.persist(clock.now())
    assert usage(conn)[1] == pytest.approx(30, abs=1)


async def test_device_play_needs_no_chromecast(conn, clock, episodes):  # PB-7
    ctrl = CastController(conn, clock=clock, tz=TZ, media_base_url="http://tv.test:8080", secret=b"s")
    await ctrl.start(run_loops=False)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await watch(ctrl, clock, DEV, 30)
    ctrl.persist(clock.now())
    assert usage(conn)[1] == pytest.approx(20, abs=1)
    assert ctrl.state()["device"] is None and ctrl.state()["sessions"][0]["target"] == "device"


async def test_resume_from_the_saved_position(conn, clock, fake, episodes):  # PB-4
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await beat(ctrl, clock, DEV, "playing", 123)
    await ctrl.device_stop(DEV)
    assert store.get_position(conn, 1, episodes[0]) == (123, False)
    again = await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    assert again["start_s"] == 123


async def test_play_validation(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    with pytest.raises(KeyError):
        await ctrl.device_play(DEV, "x", 9999, [1])
    with pytest.raises(UnknownProfile):
        await ctrl.device_play(DEV, "x", episodes[0], [1, 42])
    with pytest.raises(ValueError):
        await ctrl.device_play("short", "x", episodes[0], [1])
    with pytest.raises(ValueError):
        await ctrl.device_play(DEV, "x", episodes[0], [])
    assert not ctrl.device_sessions and not sessions(conn)


async def test_a_refused_pick_changes_nothing(conn, clock, fake, episodes, kids):  # PR-4
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await ctrl.override("block", profile_ids=[3])
    with pytest.raises(PlayRefused) as refused:
        await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [1, 3])
    assert refused.value.decision.reason == "blocked"
    assert ctrl.device_sessions[DEV].episode.id == episodes[0]


# --------------------------------------------------------------------------- several sessions at once (PB-8)


async def test_two_profiles_watch_on_the_tv_and_a_device_at_once(conn, clock, fake, episodes, kids):  # PB-8, A-30
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.play(episodes[0], [1])
    await pump(ctrl, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [2])
    assert ctrl.current is not None
    await run_for(ctrl, fake, clock, 5)
    for i in range(6):
        for _ in range(10):
            await run_for(ctrl, fake, clock, 1)
        await ctrl.device_heartbeat(DEV, "playing", 10 * (i + 1), EPISODE_S)
    ctrl.persist(clock.now())

    used = usage(conn)
    assert used[1] == pytest.approx(65, abs=2) and used[2] == pytest.approx(50, abs=2) and 3 not in used
    tv_row, dev_row = rows(conn, target="tv")[0], rows(conn, target="device")[0]
    assert tv_row["seconds_counted"] == pytest.approx(65, abs=2)  # each session only gets its own seconds
    assert dev_row["seconds_counted"] == pytest.approx(50, abs=2)
    state = ctrl.state()
    assert [s["key"] for s in state["sessions"]] == [TV, "device:" + DEV]
    assert {p["profile_id"]: p["watching"] for p in state["timer"]["profiles"]} == {1: True, 2: True, 3: False}
    assert state["now_playing"]["profile_ids"] == [1]  # the TV block stays the TV


async def test_a_tv_pick_ends_the_device_session_of_that_profile(conn, clock, fake, episodes, kids):  # PB-8
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [2, 3])
    await watch(ctrl, clock, DEV, 20)
    await ctrl.play(episodes[1], [2])  # the group-mate's session ends with it
    await pump(ctrl, fake)
    assert not ctrl.device_sessions
    assert rows(conn, target="device")[0]["end_reason"] == EndReason.REPLACED
    answer = await ctrl.device_heartbeat(DEV, "playing", 30, EPISODE_S)
    assert (answer["action"], answer["reason"]) == ("stop", "replaced")
    assert ctrl.current.profile_ids == [2]
    state = ctrl.state()
    assert [s["target"] for s in state["sessions"]] == ["tv"]
    assert {p["profile_id"]: p["watching"] for p in state["timer"]["profiles"]} == {1: False, 2: True, 3: False}


async def test_a_device_pick_stops_the_tv_session_of_that_profile(conn, clock, fake, episodes, kids):  # PB-8
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.play(episodes[0], [1, 2])
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 30)
    fake.calls.clear()
    await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [2])
    await pump(ctrl, fake)
    assert ctrl.current is None and ("stop",) in fake.calls  # the Chromecast was stopped like any stop
    assert rows(conn, target="tv")[0]["end_reason"] == EndReason.REPLACED
    assert ctrl.device_sessions[DEV].profile_ids == [2]
    ctrl.persist(clock.now())
    assert usage(conn)[1] == pytest.approx(30, abs=2)  # profile 1 paid for the TV time it had


async def test_a_device_pick_replaces_the_devices_own_session(conn, clock, fake, episodes):  # PB-8
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [1])
    assert [r["end_reason"] for r in sessions(conn)] == [EndReason.REPLACED, None]
    assert ctrl.device_sessions[DEV].episode.id == episodes[1] and len(ctrl.device_sessions) == 1


async def test_two_devices_for_two_profiles_and_a_move_between_them(conn, clock, fake, episodes, kids):  # PB-8
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await ctrl.device_play(DEV2, "Pixel Chrome", episodes[1], [2])
    assert set(ctrl.device_sessions) == {DEV, DEV2}
    await ctrl.device_play(DEV2, "Pixel Chrome", episodes[2], [1])  # profile 1 moves to the second device
    assert set(ctrl.device_sessions) == {DEV2}
    assert (await ctrl.device_heartbeat(DEV, "playing", 5))["reason"] == "replaced"


# --------------------------------------------------------------------------- lost connection (WT-10)


async def test_no_heartbeat_for_30_seconds_stops_counting_and_5_minutes_end_it(conn, clock, fake, episodes):  # WT-10
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await watch(ctrl, clock, DEV, 30)  # counts 20 s
    last_beat = clock.now()
    for _ in range(60):  # a minute of silence, ticking each second
        clock.advance(1)
        await ctrl.tick()
    ctrl.persist(clock.now())
    assert usage(conn)[1] == pytest.approx(50, abs=1)  # 20 s + the 30 s before the silence was noticed
    assert ctrl.timer.activity_of("device:" + DEV).value == "stopped"
    answer = await ctrl.device_heartbeat(DEV, "playing", 31, EPISODE_S)  # it comes back: counting resumes
    assert answer["action"] == "continue"
    assert ctrl.timer.activity_of("device:" + DEV).value == "playing"

    last_beat = clock.now()
    for _ in range(301):
        clock.advance(1)
        await ctrl.tick()
    assert not ctrl.device_sessions
    row = rows(conn, target="device")[0]
    assert row["end_reason"] == EndReason.DISCONNECTED and row["ended_at"] == to_db(last_beat)
    assert store.get_position(conn, 1, episodes[0])[0] == pytest.approx(31)  # kept up to the last heartbeat
    assert (await ctrl.device_heartbeat(DEV, "playing", 40))["reason"] == "disconnected"


async def test_a_paused_tab_that_stays_silent_ends_after_5_minutes(conn, clock, fake, episodes):  # WT-10
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await beat(ctrl, clock, DEV, "paused", 5)
    clock.advance(301)
    await ctrl.tick()
    assert rows(conn, target="device")[0]["end_reason"] == EndReason.DISCONNECTED


# --------------------------------------------------------------------------- limits (WT-11)


async def test_allowance_runs_out_then_grace_then_stop(conn, clock, fake, episodes):  # WT-4, WT-11
    configure(conn, allowance_min=1, grace_min=5)
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    answers = [await beat(ctrl, clock, DEV, "playing", 10 * (i + 1)) for i in range(8)]  # 80 s
    assert answers[0]["action"] == "continue" and not answers[0]["time_up"]
    last = answers[-1]  # the allowance ran out 60 s after the first playing heartbeat
    assert last["action"] == "continue" and last["time_up"] and last["grace_deadline"] is not None
    assert ctrl.device_sessions  # finish the episode

    for i in range(1, 4):  # 30 s more and still in grace
        assert (await beat(ctrl, clock, DEV, "playing", 80 + 10 * i))["action"] == "continue"
    for i in range(4, 40):  # the grace (5 min) ends 300 s after the allowance ran out
        answer = await beat(ctrl, clock, DEV, "playing", 80 + 10 * i)
        if answer["action"] == "stop":
            break
    assert (answer["action"], answer["reason"], answer["time_up"]) == ("stop", "time_up", True)
    assert not ctrl.device_sessions and rows(conn, target="device")[0]["end_reason"] == EndReason.TIME_UP
    assert (await ctrl.device_heartbeat(DEV, "playing", 500))["reason"] == "time_up"


async def test_the_heartbeat_itself_answers_stop_once_grace_is_over(conn, clock, fake, episodes):  # WT-5
    configure(conn, allowance_min=1, grace_min=1)
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await beat(ctrl, clock, DEV, "playing", 10)
    clock.advance(25)
    await ctrl.device_heartbeat(DEV, "playing", 35, EPISODE_S)
    clock.advance(25)
    await ctrl.device_heartbeat(DEV, "playing", 60, EPISODE_S)  # allowance out
    clock.advance(25)
    mid = await ctrl.device_heartbeat(DEV, "playing", 85, EPISODE_S)
    assert mid["action"] == "continue" and mid["time_up"]
    clock.advance(50)  # the grace (60 s from 70 s) is over; no tick ran, so the heartbeat decides
    final = await ctrl.device_heartbeat(DEV, "playing", 110, EPISODE_S)
    assert (final["action"], final["reason"]) == ("stop", "time_up")


async def test_a_session_may_not_start_when_time_is_up(conn, clock, fake, episodes):  # KA-9
    configure(conn, allowance_min=1)
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await watch(ctrl, clock, DEV, 80)
    await ctrl.device_stop(DEV)
    with pytest.raises(PlayRefused):
        await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [1])


async def test_block_stops_a_device_session_at_once(conn, clock, fake, episodes, kids):  # WT-7, WT-12
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await ctrl.device_play(DEV2, "Pixel Chrome", episodes[0], [2])
    await watch(ctrl, clock, DEV, 20)
    await ctrl.override("block", profile_ids=[1])
    assert set(ctrl.device_sessions) == {DEV2}
    assert rows(conn, target="device")[0]["end_reason"] == EndReason.BLOCKED
    answer = await ctrl.device_heartbeat(DEV, "playing", 30)
    assert (answer["action"], answer["reason"]) == ("stop", "blocked")


async def test_stop_now_ends_every_session_at_once(conn, clock, fake, episodes, kids):  # WT-7, WT-12
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.play(episodes[0], [1])
    await pump(ctrl, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [2])
    await watch(ctrl, clock, DEV, 20)
    await ctrl.override("stop_now")
    assert ctrl.current is None and not ctrl.device_sessions
    assert {r["end_reason"] for r in sessions(conn)} == {EndReason.PARENT_STOP}
    assert (await ctrl.device_heartbeat(DEV, "playing", 30))["reason"] == "stop_now"
    assert ctrl.state()["sessions"] == []


async def test_a_deleted_profile_leaves_a_device_session(conn, clock, fake, episodes, kids):  # PR-1
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [2, 3])
    conn.execute("DELETE FROM profile WHERE id = 3")
    await beat(ctrl, clock, DEV, "playing", 10)
    assert ctrl.device_sessions[DEV].profile_ids == [2]


# --------------------------------------------------------------------------- ending an episode (PB-3, PB-8, PB-9)


async def test_ended_plays_the_next_episode_and_marks_the_first_finished(conn, clock, fake, episodes):  # PB-3, PB-4
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await watch(ctrl, clock, DEV, 330)  # more than half really played
    answer = await beat(ctrl, clock, DEV, "ended", EPISODE_S)
    assert answer["action"] == "next" and answer["reason"] is None
    nxt = answer["next"]
    assert nxt["session"]["episode_id"] == episodes[1] and nxt["start_s"] == 0
    assert nxt["url"].endswith(f"?s={sessions(conn)[1]['id']}") and nxt["url"].startswith(f"/media/{episodes[1]}/")
    assert store.get_position(conn, 1, episodes[0])[1] is True
    assert [r["end_reason"] for r in sessions(conn)] == [EndReason.FINISHED, None]
    assert rows(conn, id=sessions(conn)[1]["id"])[0]["device_label"] == "iPhone Safari"
    assert ctrl.device_sessions[DEV].episode.id == episodes[1]


async def test_ended_on_the_last_episode_stops(conn, clock, fake, episodes):  # PB-3
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[2], [1])
    await watch(ctrl, clock, DEV, 330)
    answer = await beat(ctrl, clock, DEV, "ended", EPISODE_S)
    assert (answer["action"], answer["reason"]) == ("stop", "finished")
    assert not ctrl.device_sessions


async def test_ended_without_autoplay_stops(conn, clock, fake, episodes):  # PB-3
    show = library.get_episode(conn, episodes[0]).show_id
    conn.execute("UPDATE show SET autoplay = 0 WHERE id = ?", (show,))
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    answer = await beat(ctrl, clock, DEV, "ended", EPISODE_S)
    assert (answer["action"], answer["reason"]) == ("stop", "finished")


async def test_ended_in_grace_does_not_autoplay(conn, clock, fake, episodes):  # WT-4
    configure(conn, allowance_min=1, grace_min=15)
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await watch(ctrl, clock, DEV, 80)
    answer = await beat(ctrl, clock, DEV, "ended", EPISODE_S)
    assert (answer["action"], answer["reason"], answer["time_up"]) == ("stop", "time_up", True)
    assert [r["target"] for r in sessions(conn)] == ["device"]  # no second session


async def test_dragging_to_the_end_does_not_finish_the_episode(conn, clock, fake, episodes):  # PB-8, A-29
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await beat(ctrl, clock, DEV, "playing", 5)
    await beat(ctrl, clock, DEV, "playing", EPISODE_S - 1)  # a seek to the very end
    await beat(ctrl, clock, DEV, "ended", EPISODE_S)
    position, finished = store.get_position(conn, 1, episodes[0])
    assert position == EPISODE_S and finished is False


async def test_stopping_near_the_end_after_enough_play_marks_it_finished(conn, clock, fake, episodes):  # PB-4, PB-8
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[2], [1])
    await watch(ctrl, clock, DEV, 330)
    await beat(ctrl, clock, DEV, "playing", EPISODE_S - 5)
    await ctrl.device_stop(DEV)
    assert store.get_position(conn, 1, episodes[2])[1] is True


async def test_an_error_ends_the_session_without_counting(conn, clock, fake, episodes):  # PB-9
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    answer = await beat(ctrl, clock, DEV, "error", 0, after=4)
    assert (answer["action"], answer["reason"]) == ("stop", "error")
    row = rows(conn, target="device")[0]
    assert row["end_reason"] == EndReason.LOAD_FAILED and row["seconds_counted"] == 0
    ctrl.persist(clock.now())
    assert usage(conn).get(1, 0) == 0


async def test_unknown_session_heartbeat_and_stop(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    answer = await ctrl.device_heartbeat(DEV, "playing", 1)
    assert (answer["action"], answer["reason"]) == ("stop", "unknown_session")
    await ctrl.device_stop(DEV)  # a no-op
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await ctrl.device_stop(DEV)
    assert rows(conn, target="device")[0]["end_reason"] == EndReason.STOPPED and not ctrl.device_sessions
    assert (await ctrl.device_heartbeat(DEV, "playing", 1))["reason"] == "unknown_session"


# --------------------------------------------------------------------------- restart and state


async def test_a_restart_closes_device_sessions_without_recovering(conn, clock, fake, episodes):  # NF-7, PB-8
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await watch(ctrl, clock, DEV, 20)
    ctrl.persist(clock.now())
    again = await make_controller(conn, clock, fake)
    assert again._recovering is None and not again.device_sessions
    row = rows(conn, target="device")[0]
    assert row["end_reason"] == EndReason.RESTART and row["ended_at"] is not None


async def test_a_restart_still_recovers_the_tv_session_beside_a_device_session(conn, clock, fake, episodes, kids):  # NF-7
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.play(episodes[0], [1])
    await pump(ctrl, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [2])
    again = await make_controller(conn, clock, fake)
    assert again.current is not None and again.current.episode.id == episodes[0]  # re-attached
    assert not again.device_sessions
    assert rows(conn, target="device")[0]["end_reason"] == EndReason.RESTART


async def test_state_lists_the_sessions(conn, clock, fake, episodes, kids):  # PB-8, WT-12
    ctrl = await make_controller(conn, clock, fake)
    assert ctrl.state()["sessions"] == []
    await ctrl.play(episodes[0], [1])
    await pump(ctrl, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[1], [2, 3])
    await beat(ctrl, clock, DEV, "buffering", 12)
    state = ctrl.state()
    tv, dev = state["sessions"]
    assert tv["key"] == "tv" and tv["target"] == "tv" and tv["label"] == fake.info.name and tv["device_id"] is None
    assert tv["episode_id"] == episodes[0] and tv["profile_ids"] == [1]
    assert dev == {
        "key": "device:" + DEV, "target": "device", "label": "iPhone Safari", "device_id": DEV,
        "episode_id": episodes[1], "show_id": library.get_episode(conn, episodes[1]).show_id,
        "title": "Episode 2", "state": "buffering", "position_s": 12, "duration_s": EPISODE_S,
        "profile_ids": [2, 3],
    }
    assert {k: v for k, v in tv.items() if k not in ("key", "target", "label", "device_id")} == state["now_playing"]


async def test_state_changes_are_broadcast(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    queue = ctrl.subscribe()
    queue.get_nowait()
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    assert queue.get_nowait()["sessions"][0]["key"] == "device:" + DEV
