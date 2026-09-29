"""The Tellybox receiver in the cast controller (v7, CR-1..CR-8), with a fake clock and a fake Chromecast.

A made-up app id stands for the household's own receiver; no app id configured means the v1 behaviour."""

import logging

import pytest

from tellybox import library
from tellybox.cast.controller import (
    NIGHT_HOLD_S,
    RECEIVER_FALLBACK_S,
    EndReason,
    PlayRefused,
)
from tellybox.cast.fake import YOUTUBE_APP, FakeCastDevice

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
)

TB = "ABCD1234"
BASE = "http://tv.test:8080"


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
    assert ctrl.state()["receiver"] == {"kind": "default", "configured": False, "fallback_until": None, "last_error": None}


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
    assert ctrl.state()["receiver"] == {"kind": "tellybox", "configured": True, "fallback_until": None, "last_error": None}
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


async def test_launch_failure_plays_the_same_pick_on_the_default_receiver(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert fake.play_app_ids == [TB, None]
    assert len(play_calls(fake)) == 2 and play_calls(fake)[0][1] == play_calls(fake)[1][1]
    assert ctrl.current is not None and ctrl.current.cast_session_id == "dmr-1"
    receiver = ctrl.state()["receiver"]
    assert receiver["kind"] == "default" and receiver["configured"] is True
    assert receiver["fallback_until"] == "2026-09-28T14:30:00.000+00:00"
    assert "launch timed out" in receiver["last_error"]
    await run_for(ctrl, fake, clock, 60)
    assert ctrl.current is not None and fake.sent_messages == []  # nothing is sent to the Default Media Receiver


async def test_fallback_lasts_thirty_minutes_then_we_try_again(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 5)

    clock.advance(RECEIVER_FALLBACK_S - 60)
    await play(ctrl, fake, episodes[1])
    assert fake.play_app_ids == [TB, None, None]  # inside the window: no new attempt
    assert ctrl.state()["receiver"]["kind"] == "default"

    clock.advance(61)
    fake.fail_launch = False
    await play(ctrl, fake, episodes[2])
    assert fake.play_app_ids == [TB, None, None, TB]
    receiver = ctrl.state()["receiver"]
    assert receiver == {"kind": "tellybox", "configured": True, "fallback_until": None, "last_error": None}
    await run_for(ctrl, fake, clock, 10)  # DMR was running warm; moving to our app must not look like a take-over
    assert ctrl.current is not None and ctrl.current.episode.id == episodes[2]
    assert ctrl.current.cast_session_id == "tellybox-1"


async def test_failing_again_after_the_window_restarts_it(conn, clock, fake, episodes):  # CR-6
    enable(conn)
    fake.fail_launch = True
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    clock.advance(RECEIVER_FALLBACK_S + 1)
    await play(ctrl, fake, episodes[1])
    assert fake.play_app_ids == [TB, None, TB, None]
    assert ctrl.state()["receiver"]["fallback_until"] is not None


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
