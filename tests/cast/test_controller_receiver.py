"""The Tellybox receiver in the cast controller (v7, CR-1..CR-8), with a fake clock and a fake Chromecast.

A made-up app id stands for the household's own receiver; no app id configured means the v1 behaviour."""

import logging

import pytest

from tellybox import library
from tellybox.cast.controller import (
    NIGHT_HOLD_S,
    RECEIVER_FALLBACK_S,
    RECEIVER_VANISH_SETTLE_S,
    EndReason,
    PlayRefused,
)
from tellybox.cast.fake import YOUTUBE_APP, FakeCastDevice

SETTLE = RECEIVER_VANISH_SETTLE_S + 2  # past the wait for another app after a vanish

from test_controller import (  # noqa: F401  (fixtures)
    EPISODE_S,
    clock,
    configure,
    conn,
    episodes,
    fake,
    make_controller,
    play,
    play_calls,
    pump,
    run_for,
    sessions,
    used_s,
)

TB = "ABCD1234"
BASE = "http://tv.test:8080"
QUIET = {"failures_24h": 0, "launches_24h": 0, "last_failure": None, "refused": False}  # CR-6: nothing went wrong


def enable(conn, app_id=TB):
    conn.execute("UPDATE settings SET receiver_app_id = ? WHERE id = 1", (app_id,))


def states(fake):
    return [m for m in fake.sent_messages if m["type"] == "state"]


def call_names(fake):
    return [c[0] if c[0] != "receiver_message" else "msg" for c in fake.calls]


# --------------------------------------------------------------------------- no receiver configured


async def test_without_app_id_nothing_changes(conn, clock, fake, episodes):  # CR-6
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 90)
    fake.hello()  # even a stray hello gets no answer
    await pump(ctrl, fake)
    assert fake.play_app_ids == [None] and fake.sent_messages == []
    assert not any(c[0] in ("receiver_message", "stop_media") for c in fake.calls)
    assert ctrl.state()["receiver"] == {"kind": "default", "configured": False, "fallback_until": None, "last_error": None,
                                        **QUIET}


async def test_empty_app_id_is_not_configured(conn, clock, fake, episodes):
    enable(conn, "  ")
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [None] and ctrl.state()["receiver"]["configured"] is False


async def test_app_id_change_is_picked_up_on_persist(conn, clock, fake, episodes):  # AD-2
    ctrl = await make_controller(conn, clock, fake)
    enable(conn, "abcd1234")
    ctrl.persist(clock.now())
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [TB]  # stored uppercase
    assert ctrl.state()["receiver"]["configured"] is True


# --------------------------------------------------------------------------- playing on our receiver (WT-9)


async def test_pick_plays_on_our_receiver_and_counts_time(conn, clock, fake, episodes):  # CR-1, WT-2, WT-9
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [TB]
    assert ctrl.current.cast_session_id == "tellybox-1"
    assert ctrl.state()["receiver"] == {"kind": "tellybox", "configured": True, "fallback_until": None, "last_error": None,
                                        **QUIET, "launches_24h": 1}
    await run_for(ctrl, fake, clock, 60)
    ctrl.persist(clock.now())
    assert ctrl.current is not None and ctrl.state()["now_playing"]["state"] == "playing"
    assert conn.execute("SELECT seconds_used FROM daily_usage").fetchone()[0] == pytest.approx(60, abs=1)


async def test_a_second_pick_reuses_the_warm_receiver(conn, clock, fake, episodes):  # CR-1, A-5
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 5)
    await play(ctrl, fake, episodes[1])
    await run_for(ctrl, fake, clock, 5)
    assert ctrl.current.episode.id == episodes[1] and ctrl.current.cast_session_id == "tellybox-1"
    assert [s["end_reason"] for s in sessions(conn)] == [EndReason.REPLACED, None]


async def test_a_foreign_app_still_takes_over(conn, clock, fake, episodes):  # WT-9
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 10)
    fake.takeover(YOUTUBE_APP)
    await pump(ctrl, fake)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER


async def test_foreign_media_in_our_receiver_is_not_ours(conn, clock, fake, episodes):  # WT-9
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    fake.takeover(TB, content_id="http://elsewhere/video.mp4")  # same app id, but not our /media/ URL
    await pump(ctrl, fake)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER


async def test_restart_reattaches_on_our_receiver(conn, clock, fake, episodes):  # NF-7, WT-9
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 40)
    url, session_id = ctrl.current.url, ctrl.current.watch_session_id
    clock.advance(20)
    fresh = FakeCastDevice(clock, default_duration_s=EPISODE_S)
    fresh.preload(url, position_s=60, app_id=TB)  # the TV still plays it in our app
    ctrl2 = await make_controller(conn, clock, fresh)
    assert ctrl2.current is not None and ctrl2.current.watch_session_id == session_id
    assert ctrl2.current.cast_session_id == "tellybox-1"
    await run_for(ctrl2, fresh, clock, 10)
    assert ctrl2.current is not None  # the receiver session is recognised as ours


# --------------------------------------------------------------------------- fallback (CR-6)


def events(conn, *kinds):
    rows = conn.execute("SELECT * FROM receiver_event ORDER BY id").fetchall()
    return [r for r in rows if not kinds or r["kind"] in kinds]


def fallback_in(ctrl, clock):
    return None if ctrl.fallback_until is None else (ctrl.fallback_until - clock.now()).total_seconds()


async def test_launch_failure_plays_the_same_pick_on_the_default_receiver(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [TB, TB, None]  # two attempts, then the Default Media Receiver
    assert len(play_calls(fake)) == 3 and play_calls(fake)[0][1] == play_calls(fake)[2][1]
    assert ctrl.current is not None and ctrl.current.cast_session_id == "dmr-1"
    receiver = ctrl.state()["receiver"]
    assert receiver["kind"] == "default" and receiver["configured"] is True
    assert receiver["fallback_until"] is None  # the first failed pick: the next one tries again
    assert "launch timed out" in receiver["last_error"]
    assert receiver["failures_24h"] == 2 and receiver["refused"] is False
    assert receiver["last_failure"]["kind"] == "launch_failed" and "attempt 2" in receiver["last_failure"]["detail"]
    assert [e["kind"] for e in events(conn)] == ["launch_failed", "launch_failed", "fallback"]
    await run_for(ctrl, fake, clock, 60)
    assert ctrl.current is not None and fake.sent_messages == []  # nothing is sent to the Default Media Receiver


async def test_slow_first_launch_is_retried_on_our_receiver(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.launch_durations = [20.0]  # the first attempt (8 s) times out; the second (15 s) finds it fast
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [TB, TB] and fake.launch_timeouts == [8.0, 15.0]
    assert ("quit_app",) in fake.calls  # the half-started app was cleared first
    await run_for(ctrl, fake, clock, 10)
    assert ctrl.current is not None and ctrl.current.cast_session_id == "tellybox-2"  # no stale status took it over
    assert ctrl.state()["receiver"]["kind"] == "tellybox" and fake.sent_messages
    failed, ok = events(conn, "launch_failed", "launch_ok")
    assert failed["kind"] == "launch_failed" and failed["duration_ms"] == 8000 and "attempt 1" in failed["detail"]
    assert ok["kind"] == "launch_ok" and "attempt 2" in ok["detail"] and ok["episode_id"] == episodes[0]
    assert not events(conn, "fallback") and ctrl.fallback_until is None and ctrl.receiver_failures == 0


async def test_a_refusal_is_not_retried_and_falls_back_for_thirty_minutes(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.refuse_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [TB, None]
    assert fallback_in(ctrl, clock) == RECEIVER_FALLBACK_S
    receiver = ctrl.state()["receiver"]
    assert receiver["kind"] == "default" and receiver["refused"] is True
    assert receiver["last_failure"]["kind"] == "refused" and receiver["failures_24h"] == 1
    assert [e["kind"] for e in events(conn)] == ["refused", "fallback"]
    clock.advance(RECEIVER_FALLBACK_S + 1)
    assert ctrl.state()["receiver"]["refused"] is False  # the fallback is over


async def test_fallback_backs_off_with_each_failed_pick(conn, clock, fake, episodes):  # CR-6
    configure(conn, allowance_min=1000, max_session_min=1000)  # the clock jumps over hours
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    expected = [None, 300.0, 900.0, 1800.0, 1800.0]  # the 1st failed pick: none; 2nd 5 min; 3rd 15; 4th and later 30
    for i, delay in enumerate(expected):
        fake.play_app_ids.clear()
        await play(ctrl, fake, episodes[i % 3])
        assert fake.play_app_ids == [TB, TB, None]
        assert fallback_in(ctrl, clock) == delay and ctrl.receiver_failures == i + 1
        if delay:
            clock.advance(delay - 10)  # still inside the window: no attempt at all
            fake.play_app_ids.clear()
            await play(ctrl, fake, episodes[(i + 1) % 3])
            assert fake.play_app_ids == [None]
            clock.advance(11)
    assert [e["kind"] for e in events(conn, "fallback")] == ["fallback"] * 5


async def test_the_next_pick_after_one_failed_pick_tries_our_receiver_again(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    fake.fail_launch = False
    fake.play_app_ids.clear()
    await play(ctrl, fake, episodes[1])
    assert fake.play_app_ids == [TB]  # no waiting period after a single failure
    await run_for(ctrl, fake, clock, 10)  # DMR was running warm; moving to our app must not look like a take-over
    assert ctrl.current is not None and ctrl.current.cast_session_id == "tellybox-1"
    assert ctrl.state()["receiver"]["kind"] == "tellybox" and ctrl.receiver_failures == 0


async def test_a_success_resets_the_failure_count(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await play(ctrl, fake, episodes[1])
    assert ctrl.receiver_failures == 2 and fallback_in(ctrl, clock) == 300.0
    clock.advance(301)
    fake.fail_launch = False
    await play(ctrl, fake, episodes[2])
    assert ctrl.receiver_failures == 0 and ctrl.fallback_until is None
    fake.fail_launch = True
    await ctrl.stop()  # the receiver is gone again
    await pump(ctrl, fake)
    await play(ctrl, fake, episodes[0])
    assert ctrl.receiver_failures == 1 and ctrl.fallback_until is None  # back to the first rung of the ladder


async def test_the_autoplay_boundary_after_a_one_off_fallback_goes_back_to_our_receiver(conn, clock, fake, episodes):  # CR-6, PB-3
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [TB, TB, None]
    fake.fail_launch = False
    await run_for(ctrl, fake, clock, EPISODE_S)  # episode 1 ends on the DMR and episode 2 autoplays
    assert ctrl.current is not None and ctrl.current.episode.id == episodes[1]
    assert fake.play_app_ids[-1] == TB and ctrl.current.cast_session_id == "tellybox-1"
    await run_for(ctrl, fake, clock, 10)
    assert ctrl.current is not None and ctrl.state()["receiver"]["kind"] == "tellybox"


async def test_failing_again_after_the_window_restarts_it(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.refuse_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    clock.advance(RECEIVER_FALLBACK_S + 1)
    await play(ctrl, fake, episodes[1])
    assert fake.play_app_ids == [TB, None, TB, None]
    assert fallback_in(ctrl, clock) == RECEIVER_FALLBACK_S


# --------------------------------------------------------------------------- mid-episode recovery (CR-6, WT-9)


async def test_a_vanished_receiver_is_relaunched_at_the_position(conn, clock, fake, episodes):  # CR-6, WT-3
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 60)
    watch_id = ctrl.current.watch_session_id
    fake.vanish()  # stopped from a phone's Google Home: the Backdrop, not another app
    await pump(ctrl, fake)
    assert ctrl.current is not None  # not ended at once: it may be the step before another app
    await run_for(ctrl, fake, clock, SETTLE)
    assert ctrl.current is not None and ctrl.current.watch_session_id == watch_id
    assert len(play_calls(fake)) == 2 and play_calls(fake)[1][2] == pytest.approx(60, abs=1)
    assert fake.play_app_ids == [TB, TB] and ctrl.current.cast_session_id == "tellybox-2"
    assert ctrl.state()["now_playing"]["state"] == "playing"
    assert [s["end_reason"] for s in sessions(conn)] == [None]  # one session throughout
    await run_for(ctrl, fake, clock, 60)
    assert ctrl.current is not None and 118 <= ctrl.state()["now_playing"]["position_s"] <= 128
    ctrl.persist(clock.now())
    assert 118 <= used_s(conn) <= 128  # the few seconds the TV showed nothing are not counted
    lost, recovered = events(conn, "lost", "recovered")
    assert lost["episode_id"] == episodes[0] and "at 60 s" in lost["detail"]
    assert recovered["kind"] == "recovered"


async def test_a_second_vanish_in_the_same_episode_ends_it(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    fake.vanish()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, SETTLE)
    assert len(play_calls(fake)) == 2 and ctrl.current is not None
    await run_for(ctrl, fake, clock, 20)
    fake.vanish()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, SETTLE)
    assert ctrl.current is None and len(play_calls(fake)) == 2
    assert sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER
    assert len(events(conn, "lost")) == 2 and len(events(conn, "recovered")) == 1


async def test_another_app_is_still_a_takeover(conn, clock, fake, episodes):  # WT-9
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20)
    fake.takeover(YOUTUBE_APP)  # a null status, then the YouTube app
    await pump(ctrl, fake)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER
    await run_for(ctrl, fake, clock, 10)
    assert len(play_calls(fake)) == 1 and not events(conn, "lost", "recovered")


async def test_another_app_right_after_the_backdrop_is_still_a_takeover(conn, clock, fake, episodes):  # WT-9
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20)
    fake.vanish()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 1)
    fake.takeover(YOUTUBE_APP)  # inside the settle time
    await pump(ctrl, fake)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER
    await run_for(ctrl, fake, clock, 10)
    assert len(play_calls(fake)) == 1 and not events(conn, "lost", "recovered")


async def test_a_slow_cold_start_of_another_app_is_not_cut_off(conn, clock, fake, episodes):  # WT-9
    """A 1st gen shows no app for seconds while it cold-starts e.g. YouTube; relaunching ours then would end that cast."""
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20)
    fake.vanish()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 8)
    assert len(play_calls(fake)) == 1  # still waiting: no relaunch yet
    fake.takeover(YOUTUBE_APP)
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, SETTLE)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER
    assert len(play_calls(fake)) == 1 and not events(conn, "recovered")


async def test_no_recovery_when_time_is_up(conn, clock, fake, episodes):  # CR-6, WT-5
    configure(conn, allowance_min=5)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 310)  # the allowance is gone: the episode finishes, then stops
    assert ctrl.current is not None and not ctrl.state()["timer"]["can_start"]
    fake.vanish()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, SETTLE)
    assert ctrl.current is None and len(play_calls(fake)) == 1
    assert sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER
    assert not events(conn, "recovered")


async def test_no_recovery_after_our_own_stop(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20)
    await ctrl.stop()  # quits our app: the same Backdrop, but our doing
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 10)
    assert ctrl.current is None and len(play_calls(fake)) == 1
    assert sessions(conn)[0]["end_reason"] == EndReason.STOPPED and not events(conn, "lost")


async def test_no_recovery_during_the_night_hold(conn, clock, fake, episodes):  # CR-3, CR-6
    configure(conn, allowance_min=5)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 310 + EPISODE_S)  # finished into the night screen
    assert ctrl.current is None and fake.receiver_running
    await run_for(ctrl, fake, clock, NIGHT_HOLD_S + 5)  # the hold ends and our app quits (the Backdrop)
    assert ("stop",) in fake.calls and ctrl.current is None
    assert len(play_calls(fake)) == 1 and not events(conn, "lost", "recovered")


async def test_a_power_cycle_is_a_disconnect_not_a_vanish(conn, clock, fake, episodes):  # PB-5, CR-6
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20)
    fake.power_cycle()
    await pump(ctrl, fake)
    fake.reconnect()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 10)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.DISCONNECTED
    assert len(play_calls(fake)) == 1 and not events(conn, "lost", "recovered")


async def test_a_failed_relaunch_ends_the_episode(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20)
    fake.vanish()
    await pump(ctrl, fake)
    fake.fail_next_command = True  # the device refuses the load itself
    await run_for(ctrl, fake, clock, SETTLE)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.TAKEN_OVER
    failed = events(conn, "recover_failed")
    assert len(failed) == 1 and "injected failure" in failed[0]["detail"]


async def test_a_pick_during_the_settle_time_wins(conn, clock, fake, episodes):  # CR-6, A-5
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 20)
    fake.vanish()
    await pump(ctrl, fake)
    await play(ctrl, fake, episodes[1])  # a kid picks before the settle time has passed
    await run_for(ctrl, fake, clock, 10)
    assert ctrl.current.episode.id == episodes[1] and len(play_calls(fake)) == 2
    assert not events(conn, "recovered")


# --------------------------------------------------------------------------- receiver page reports (CR-6, CR-7)


async def test_hello_with_sdk_retries_is_recorded(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    fake.receiver_message({"type": "hello", "v": 1, "ua": "X", "sdk_attempts": 1, "load_ms": 900})
    fake.receiver_message({"type": "hello", "v": 1, "ua": "X", "sdk_attempts": 3, "load_ms": 7400})
    fake.receiver_message({"type": "hello", "v": 1, "ua": "old receiver"})  # the protocol without the new fields
    fake.receiver_message({"type": "log", "v": 1, "level": "info", "msg": "image ok"})
    fake.receiver_message({"type": "log", "v": 1, "level": "error", "msg": "JS error: boom"})
    await pump(ctrl, fake)
    first, second = events(conn, "page_error")
    assert "3 attempts" in first["detail"] and "7400" in first["detail"]
    assert second["detail"] == "JS error: boom"
    assert ctrl.state()["receiver"]["last_failure"]["kind"] == "page_error"


# --------------------------------------------------------------------------- state messages (CR-2, CR-7)


async def test_hello_gets_the_full_state(conn, clock, fake, episodes):  # CR-7
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])  # the cold launch says hello on its own
    fake.sent_messages.clear()
    fake.hello()
    await pump(ctrl, fake)
    (msg,) = fake.sent_messages
    assert msg["type"] == "state" and msg["v"] == 1
    assert msg["sky"] == {"fraction_left": 1.0, "last_five": False, "unlimited": False}
    assert msg["time_up"] is False and msg["up_next"] == {"thumb": f"{BASE}/img/episode/{episodes[1]}.jpg"}


async def test_loading_is_sent_after_a_cold_launch_and_cleared_when_playing(conn, clock, fake, episodes):  # CR-4
    enable(conn)
    conn.execute("UPDATE show SET artwork_path = 'shows/1.jpg'")
    ctrl = await make_controller(conn, clock, fake)
    show_id = library.get_episode(conn, episodes[0]).show_id
    await play(ctrl, fake, episodes[0])
    first = states(fake)[0]
    assert first["loading"] == {"artwork": f"{BASE}/img/show/{show_id}.jpg", "thumb": f"{BASE}/img/episode/{episodes[0]}.jpg"}
    assert states(fake)[-1]["loading"] is None  # PLAYING clears it


async def test_loading_without_artwork_is_null(conn, clock, fake, episodes):  # CR-4
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert states(fake)[0]["loading"]["artwork"] is None


async def test_loading_is_sent_before_each_load_including_autoplay(conn, clock, fake, episodes):  # CR-4, PB-3
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 5)
    await play(ctrl, fake, episodes[1])  # warm now: the message is delivered before the load
    names = call_names(fake)
    loading = [i for i, c in enumerate(fake.calls) if c[0] == "receiver_message" and c[1]["loading"]]
    plays = [i for i, n in enumerate(names) if n == "play"]
    assert loading[-1] < plays[1]
    assert fake.calls[loading[-1]][1]["loading"]["thumb"].endswith(f"/{episodes[1]}.jpg")

    await run_for(ctrl, fake, clock, EPISODE_S)  # episode 2 ends, episode 3 autoplays
    assert ctrl.current.episode.id == episodes[2]
    loading = [i for i, c in enumerate(fake.calls) if c[0] == "receiver_message" and c[1]["loading"]]
    plays = [i for i, n in enumerate(call_names(fake)) if n == "play"]
    assert loading[-1] < plays[-1]
    assert fake.calls[loading[-1]][1]["loading"]["thumb"].endswith(f"/{episodes[2]}.jpg")


async def test_up_next_only_when_autoplay_will_continue(conn, clock, fake, episodes):  # CR-5
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert states(fake)[-1]["up_next"] == {"thumb": f"{BASE}/img/episode/{episodes[1]}.jpg"}
    await play(ctrl, fake, episodes[2])  # the last episode of the show
    assert states(fake)[-1]["up_next"] is None
    await play(ctrl, fake, episodes[1])
    assert states(fake)[-1]["up_next"] is not None
    conn.execute("UPDATE show SET autoplay = 0")  # autoplay switched off for the show
    await run_for(ctrl, fake, clock, 31)
    assert states(fake)[-1]["up_next"] is None


async def test_no_up_next_once_time_is_up(conn, clock, fake, episodes):  # CR-5, WT-4
    configure(conn, allowance_min=5)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert states(fake)[-1]["up_next"] is not None
    await run_for(ctrl, fake, clock, 305)  # allowance gone: finish this episode, then stop
    last = states(fake)[-1]
    assert last["time_up"] is True and last["up_next"] is None


async def test_push_throttle(conn, clock, fake, episodes):  # CR-7
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    n = len(states(fake))
    await run_for(ctrl, fake, clock, 25)
    assert len(states(fake)) == n  # a 60-minute day moves by less than 1% in 25 s
    await run_for(ctrl, fake, clock, 6)
    assert len(states(fake)) == n + 1  # 30 s since the last message
    await run_for(ctrl, fake, clock, 25)
    assert len(states(fake)) == n + 1


async def test_a_one_percent_move_sends_at_once(conn, clock, fake, episodes):  # CR-2
    configure(conn, allowance_min=10)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    first = states(fake)[-1]["sky"]["fraction_left"]
    await run_for(ctrl, fake, clock, 7)  # 1% of 10 minutes is 6 s
    sent = states(fake)[-1]["sky"]["fraction_left"]
    assert first - sent >= 0.01 and sent == pytest.approx(0.988, abs=0.005)


async def test_dusk_and_unlimited_are_sent_when_they_change(conn, clock, fake, episodes):  # CR-2, KA-8
    configure(conn, allowance_min=6)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert states(fake)[-1]["sky"]["last_five"] is False
    await run_for(ctrl, fake, clock, 62)
    assert states(fake)[-1]["sky"]["last_five"] is True
    await ctrl.override("unlimited")
    await pump(ctrl, fake)
    assert states(fake)[-1]["sky"] == {"fraction_left": None, "last_five": False, "unlimited": True}


async def test_sky_follows_the_watchers(conn, clock, fake, episodes):  # PR-4, CR-2
    conn.execute("INSERT INTO profile (name, daily_allowance_min, created_at) VALUES ('B', 20, '2026-09-28T00:00:00Z')")
    configure(conn, allowance_min=60)
    conn.execute("UPDATE profile SET daily_allowance_min = 20 WHERE id = 2")
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.play(episodes[0], [2])
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 60)
    assert states(fake)[-1]["sky"]["fraction_left"] < 0.97  # against 20 minutes, not the 60 of profile 1
    assert states(fake)[-1]["sky"]["fraction_left"] == pytest.approx(1 - 60 / 1200, abs=0.011)


async def test_reconnecting_receiver_is_resent_everything(conn, clock, fake, episodes):  # CR-7
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    fake.sent_messages.clear()
    fake.hello()
    await pump(ctrl, fake)
    fake.hello()
    await pump(ctrl, fake)
    assert len(states(fake)) == 2  # every hello is answered, even with nothing changed


# --------------------------------------------------------------------------- night (CR-3)


async def test_time_up_keeps_the_night_then_quits_after_ten_minutes(conn, clock, fake, episodes):  # CR-3, WT-4
    configure(conn, allowance_min=5)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 310)
    await run_for(ctrl, fake, clock, EPISODE_S - 310 + 2)  # the episode finishes, no autoplay
    assert ctrl.current is None and len(play_calls(fake)) == 1
    assert ("stop",) not in fake.calls and fake.receiver_running
    assert sessions(conn)[0]["end_reason"] == EndReason.FINISHED
    assert states(fake)[-1]["time_up"] is True and states(fake)[-1]["loading"] is None

    await run_for(ctrl, fake, clock, NIGHT_HOLD_S - 60)
    assert ("stop",) not in fake.calls and fake.receiver_running
    await run_for(ctrl, fake, clock, 70)
    assert ("stop",) in fake.calls
    assert not fake.receiver_running


async def test_night_after_a_restart_still_ends(conn, clock, fake, episodes):  # CR-3, NF-7
    configure(conn, allowance_min=5)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, EPISODE_S + 2)  # time runs out mid-episode; it finishes into the night
    await run_for(ctrl, fake, clock, NIGHT_HOLD_S - 120)
    assert ctrl.current is None and fake.receiver_running  # the night screen is up

    # A deploy restarts the cast service; the TV keeps our idle receiver on screen.
    ctrl2 = await make_controller(conn, clock, fake)
    await run_for(ctrl2, fake, clock, NIGHT_HOLD_S - 60)
    assert ("stop",) not in fake.calls and fake.receiver_running  # a fresh hold, not cut short
    await run_for(ctrl2, fake, clock, 70)
    assert ("stop",) in fake.calls and not fake.receiver_running


async def test_idle_receiver_found_on_reconnect_gets_a_night_hold(conn, clock, fake, episodes):  # CR-3
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await ctrl.override("block")  # the night screen, then the connection's state is lost
    await pump(ctrl, fake)
    ctrl._night_until = None
    await run_for(ctrl, fake, clock, 5)
    assert ctrl._night_until is not None and fake.receiver_running
    await run_for(ctrl, fake, clock, NIGHT_HOLD_S)
    assert ("stop",) in fake.calls and not fake.receiver_running


async def test_night_is_not_quit_twice_or_over_another_app(conn, clock, fake, episodes):  # CR-3, WT-9
    configure(conn, allowance_min=5)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 310 + EPISODE_S)
    fake.takeover(YOUTUBE_APP)  # a phone casts during the night
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, NIGHT_HOLD_S + 5)
    assert ("stop",) not in fake.calls and fake.receiver.app_id == YOUTUBE_APP


async def test_grace_end_keeps_the_app_and_stops_the_media(conn, clock, fake, episodes):  # CR-3, WT-5
    configure(conn, allowance_min=1, grace_min=15)
    fake.durations[f"/media/{episodes[0]}/"] = 3 * 3600
    conn.execute("UPDATE episode SET duration_s = ? WHERE id = ?", (3 * 3600, episodes[0]))
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 60 + 15 * 60 + 2)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.TIME_UP
    assert ("stop_media",) in fake.calls and ("stop",) not in fake.calls
    assert fake.receiver_running and states(fake)[-1]["time_up"] is True


async def test_block_shows_the_night(conn, clock, fake, episodes):  # CR-3, WT-7
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 10)
    await ctrl.override("block")
    await pump(ctrl, fake)
    assert ctrl.current is None and sessions(conn)[0]["end_reason"] == EndReason.BLOCKED
    assert ("stop_media",) in fake.calls and ("stop",) not in fake.calls
    assert states(fake)[-1]["time_up"] is True
    await run_for(ctrl, fake, clock, NIGHT_HOLD_S + 2)
    assert ("stop",) in fake.calls and not fake.receiver_running


async def test_a_new_pick_cancels_the_night(conn, clock, fake, episodes):  # CR-3
    configure(conn, allowance_min=5)
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 310 + EPISODE_S)
    assert ctrl.current is None and ctrl._night_until is not None
    await run_for(ctrl, fake, clock, 120)

    await ctrl.override("extra_minutes", 30)  # a parent gives more time, the kids pick again
    await play(ctrl, fake, episodes[1])
    assert ctrl._night_until is None
    assert states(fake)[-1]["time_up"] is False
    await run_for(ctrl, fake, clock, NIGHT_HOLD_S)  # the old deadline passes: the app must stay
    assert ("stop",) not in fake.calls and ctrl.current is not None


async def test_stop_without_time_up_quits_the_app(conn, clock, fake, episodes):  # CR-3, PB-5
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await ctrl.stop()
    await pump(ctrl, fake)
    assert fake.calls[-1] == ("stop",) and not fake.receiver_running and ctrl._night_until is None


async def test_parent_stop_now_with_time_left_quits_the_app(conn, clock, fake, episodes):  # WT-7
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await ctrl.override("stop_now")
    await pump(ctrl, fake)
    assert ("stop",) in fake.calls and ctrl._night_until is None


async def test_end_of_show_quits_the_app(conn, clock, fake, episodes):  # PB-3
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[2])
    await run_for(ctrl, fake, clock, EPISODE_S + 2)
    assert ctrl.current is None and ("stop",) in fake.calls and ctrl._night_until is None


async def test_default_receiver_time_up_stops_as_before(conn, clock, fake, episodes):  # CR-6
    configure(conn, allowance_min=5)
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 310 + EPISODE_S)
    assert ctrl.current is None and fake.calls[-1] == ("stop",)
    assert ctrl._night_until is None and fake.sent_messages == []
    with pytest.raises(PlayRefused):
        await ctrl.play(episodes[1])


# --------------------------------------------------------------------------- stats (CR-8)


async def test_stats_are_logged(conn, clock, fake, episodes, caplog):  # CR-8
    enable(conn)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    with caplog.at_level(logging.INFO, logger="tellybox.cast.controller"):
        fake.receiver_message({"type": "stats", "v": 1, "dropped": 12, "total": 43200, "state": "PLAYING"})
        fake.receiver_message({"type": "log", "v": 1, "level": "error", "msg": "image failed"})
        await pump(ctrl, fake)
    text = caplog.text
    assert "dropped=12" in text and "total=43200" in text
    assert any(r.levelno == logging.ERROR and "image failed" in r.message for r in caplog.records)
    assert ctrl.current is not None  # receiver chatter never touches the session
