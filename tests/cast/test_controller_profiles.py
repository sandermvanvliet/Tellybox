"""Kid profiles in the cast controller (PR-2, PR-4, A-13), with a fake clock and a fake Chromecast."""

import asyncio

import pytest

from tellybox import store
from tellybox.cast.controller import CastController, EndReason, PlayRefused, UnknownProfile
from tellybox.cast.fake import FakeCastDevice
from tellybox.db import to_db

from test_controller import (  # noqa: F401  (fixtures)
    EPISODE_S,
    clock,
    configure,
    conn,
    episodes,
    fake,
    make_controller,
    play_calls,
    pump,
    run_for,
    TZ,
    sessions,
)


@pytest.fixture
def kids(conn, clock):
    """Three profiles: 1 (the migrated household), 2 and 3."""
    for name in ("B", "C"):
        conn.execute("INSERT INTO profile (name, created_at) VALUES (?, ?)", (name, to_db(clock.now())))
    return [1, 2, 3]


def usage(conn) -> dict[int, float]:
    return {r["profile_id"]: r["seconds_used"] for r in conn.execute("SELECT * FROM daily_usage")}


def session_profiles(conn, session_id) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT profile_id FROM watch_session_profile WHERE watch_session_id = ? ORDER BY profile_id", (session_id,)
    )]


async def play_for(ctrl, fake, episode_id, profile_ids):
    await ctrl.play(episode_id, profile_ids)
    await pump(ctrl, fake)


async def test_only_the_group_is_timed_and_recorded(conn, clock, fake, episodes, kids):  # PR-2, PR-4
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [2, 3])
    await run_for(ctrl, fake, clock, 60)
    ctrl.persist(clock.now())
    used = usage(conn)
    assert used[2] == pytest.approx(60, abs=1) and used[3] == pytest.approx(60, abs=1)
    assert 1 not in used
    assert session_profiles(conn, sessions(conn)[0]["id"]) == [2, 3]
    state = ctrl.state()
    assert state["now_playing"]["profile_ids"] == [2, 3]
    assert {p["profile_id"]: p["watching"] for p in state["timer"]["profiles"]} == {1: False, 2: True, 3: True}


async def test_play_needs_known_profiles(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    with pytest.raises(ValueError):
        await ctrl.play(episodes[0], [])
    with pytest.raises(UnknownProfile):
        await ctrl.play(episodes[0], [1, 42])
    assert ctrl.current is None and not play_calls(fake)


async def test_duplicate_ids_are_one_member(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [2, 2, 3])
    assert ctrl.current.profile_ids == [2, 3]


async def test_new_pick_changes_the_group(conn, clock, fake, episodes, kids):  # A-5
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [1])
    await run_for(ctrl, fake, clock, 30)
    await play_for(ctrl, fake, episodes[1], [2])
    await run_for(ctrl, fake, clock, 30)
    ctrl.persist(clock.now())
    used = usage(conn)
    assert used[1] == pytest.approx(30, abs=2) and used[2] == pytest.approx(30, abs=2)
    assert sessions(conn)[0]["end_reason"] == EndReason.REPLACED


async def test_autoplay_keeps_the_group(conn, clock, fake, episodes, kids):  # PB-3
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [2, 3])
    await run_for(ctrl, fake, clock, EPISODE_S + 5)
    assert ctrl.current.episode.id == episodes[1]
    assert ctrl.current.profile_ids == [2, 3]
    assert session_profiles(conn, sessions(conn)[1]["id"]) == [2, 3]
    ctrl.persist(clock.now())
    assert 1 not in usage(conn)


async def test_a_refusal_carries_the_groups_reason(conn, clock, fake, episodes, kids):  # PR-4
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.override("block", profile_ids=[3])
    with pytest.raises(PlayRefused) as refused:
        await ctrl.play(episodes[0], [1, 3])
    assert refused.value.decision.reason == "blocked"
    await play_for(ctrl, fake, episodes[0], [1, 2])  # the others can still start


async def test_one_member_out_of_time_refuses_the_group(conn, clock, fake, episodes, kids):  # PR-4
    configure(conn, allowance_min=60)
    # A-23: set allowance_mode='custom' to use the custom daily_allowance_min value
    conn.execute("UPDATE profile SET allowance_mode = 'custom', daily_allowance_min = 1 WHERE id = 3")
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [3])
    await run_for(ctrl, fake, clock, 70)
    await ctrl.stop()
    with pytest.raises(PlayRefused) as refused:
        await ctrl.play(episodes[1], [1, 3])
    assert refused.value.decision.reason == "allowance"
    await play_for(ctrl, fake, episodes[1], [1])
    state = ctrl.state()
    by_id = {p["profile_id"]: p for p in state["timer"]["profiles"]}
    assert by_id[3]["can_start"] is False and by_id[3]["reason"] == "allowance" and by_id[3]["remaining_s"] == 0
    assert by_id[1]["can_start"] is True and by_id[1]["remaining_s"] > 0
    assert state["timer"]["can_start"] and not state["time_up"]  # describes the watcher, profile 1


async def test_group_finishes_the_episode_then_stops_and_the_other_can_start(conn, clock, fake, episodes, kids):
    configure(conn, allowance_min=60)
    # A-23: set allowance_mode='custom' to use the custom daily_allowance_min value
    conn.execute("UPDATE profile SET allowance_mode = 'custom', daily_allowance_min = 1 WHERE id = 2")
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [1, 2])
    await run_for(ctrl, fake, clock, 65)
    state = ctrl.state()
    assert state["timer"]["action"] == "finish_then_stop" and state["time_up"]
    await run_for(ctrl, fake, clock, EPISODE_S)
    assert ctrl.current is None and len(play_calls(fake)) == 1
    await play_for(ctrl, fake, episodes[1], [1])
    assert ctrl.current is not None


async def test_blocking_a_non_watcher_leaves_playback_alone(conn, clock, fake, episodes, kids):  # WT-7
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [1])
    await run_for(ctrl, fake, clock, 10)
    await ctrl.override("block", profile_ids=[2])
    await pump(ctrl, fake)
    assert ctrl.current is not None and ctrl.state()["timer"]["action"] == "continue"
    by_id = {p["profile_id"]: p for p in ctrl.state()["timer"]["profiles"]}
    assert by_id[2]["blocked"] and by_id[2]["reason"] == "blocked" and not by_id[2]["watching"]
    await ctrl.override("block", profile_ids=[1])  # a watcher: stops now
    await pump(ctrl, fake)
    assert ctrl.current is None


async def test_group_resumes_at_the_most_recent_unfinished_position(conn, clock, fake, episodes, kids):  # PB-4
    ep = episodes[0]
    now = clock.now()
    store.save_position(conn, [1], ep, 100, False, now)
    clock.advance(60)
    store.save_position(conn, [2], ep, 250, False, clock.now())
    clock.advance(60)
    store.save_position(conn, [3], ep, 590, True, clock.now())  # finished: never a resume point
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, ep, [1, 2, 3])
    assert play_calls(fake)[0][2] == 250
    await ctrl.stop()
    await play_for(ctrl, fake, ep, [1])
    assert play_calls(fake)[1][2] == pytest.approx(250, abs=2)  # the group saved it for every member
    await ctrl.stop()
    await play_for(ctrl, fake, episodes[2], [3])
    assert play_calls(fake)[2][2] == 0


async def test_recovery_reattaches_the_watchers(conn, clock, fake, episodes, kids):  # NF-7
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [2, 3])
    await run_for(ctrl, fake, clock, 40)
    url = ctrl.current.url
    clock.advance(20)
    fresh = FakeCastDevice(clock, default_duration_s=EPISODE_S)
    fresh.preload(url, position_s=60)
    ctrl2 = await make_controller(conn, clock, fresh)
    assert ctrl2.current.profile_ids == [2, 3]
    assert ctrl2.timer.watchers == {2, 3}
    before = usage(conn)
    await run_for(ctrl2, fresh, clock, 30)
    ctrl2.persist(clock.now())
    after = usage(conn)
    assert after[2] == pytest.approx(before[2] + 30, abs=2) and after[3] == pytest.approx(before[3] + 30, abs=2)
    assert 1 not in after


async def test_the_watchers_survive_a_restart_while_stopped(conn, clock, fake, episodes, kids):  # WT-8
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [3])
    await ctrl.stop()
    ctrl2 = await make_controller(conn, clock, FakeCastDevice(clock))
    assert ctrl2.timer.watchers == {3}


async def test_profile_deleted_mid_play_is_dropped(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [2, 3])
    await run_for(ctrl, fake, clock, 20)
    conn.execute("DELETE FROM profile WHERE id = 3")
    await run_for(ctrl, fake, clock, 20)  # ticks; the persist must not hit a foreign key error
    ctrl.persist(clock.now())
    assert ctrl.current.profile_ids == [2]
    assert ctrl.timer.watchers == {2}
    state = ctrl.state()
    assert state["now_playing"]["profile_ids"] == [2]
    assert [p["profile_id"] for p in state["timer"]["profiles"]] == [1, 2]
    await ctrl.stop()  # ending the episode saves the positions of the remaining members only
    assert store.get_position(conn, 2, episodes[0]) is not None


async def test_profile_added_while_running_can_play(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    conn.execute("INSERT INTO profile (name, created_at) VALUES ('New', ?)", (to_db(clock.now()),))
    assert [p["profile_id"] for p in ctrl.state()["timer"]["profiles"]] == [1, 2]
    await play_for(ctrl, fake, episodes[0], [2])
    assert ctrl.current.profile_ids == [2]


async def test_no_profile_list_means_everyone(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.play(episodes[0])
    assert ctrl.current.profile_ids == [1, 2, 3]


async def test_unlimited_by_policy_never_exhausts(conn, clock, fake, episodes, kids):
    """A profile with allowance_mode='unlimited' can play indefinitely (A-23)."""
    configure(conn, allowance_min=60)
    # Set profile 2 to have an unlimited allowance policy
    conn.execute("UPDATE profile SET allowance_mode = 'unlimited' WHERE id = 2")
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [2])
    # Play for longer than the default allowance (60 min)
    await run_for(ctrl, fake, clock, 120)
    # Should still be playing, not exhausted
    assert ctrl.current is not None
    state = ctrl.state()
    assert state["timer"]["action"] == "continue"
    assert not state["time_up"]
    by_id = {p["profile_id"]: p for p in state["timer"]["profiles"]}
    assert by_id[2]["remaining_s"] is None  # unlimited
    assert by_id[2]["can_start"] is True


async def test_unlimited_max_session_by_policy_allows_long_viewing(conn, clock, fake, episodes, kids):
    """A profile with max_session_mode='unlimited' can watch multiple episodes indefinitely (A-23)."""
    configure(conn, max_session_min=90)
    # Profile 1: unlimited max_session policy
    conn.execute("UPDATE profile SET max_session_mode = 'unlimited' WHERE id = 1")
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [1])
    # Play first episode to completion (EPISODE_S = 600 seconds)
    await run_for(ctrl, fake, clock, EPISODE_S + 5)
    # Autoplay should continue to next episode
    assert ctrl.current.episode.id == episodes[1]
    # Play until the second episode is nearly done
    await run_for(ctrl, fake, clock, EPISODE_S + 5)
    # Autoplay should continue to third episode
    assert ctrl.current.episode.id == episodes[2]
    # Should not be time_up (max_session is unlimited)
    state = ctrl.state()
    assert not state["time_up"]


# --------------------------------------------------------------------------- per-profile TV (PB-6)

from tellybox.cast.controller import NoDevice  # noqa: E402
from tellybox.cast.device import DeviceInfo  # noqa: E402
from tellybox.cast.pychromecast_device import CastCommandError  # noqa: E402

LIVING = DeviceInfo(uuid="00000000-0000-0000-0000-00000000000a", name="Living Room TV", host="192.0.2.10")
BEDROOM = DeviceInfo(uuid="00000000-0000-0000-0000-00000000000b", name="Bedroom TV", host="192.0.2.11")


@pytest.fixture
def tvs(conn, clock):
    """Two known TVs with the living room as the global selected one; the factory records what it made."""
    store.remember_devices(conn, [LIVING, BEDROOM], clock.now())
    store.select_device(conn, LIVING.uuid)
    made: dict[str, FakeCastDevice] = {}

    def factory(info):
        made[info.uuid] = FakeCastDevice(clock, info=info, default_duration_s=EPISODE_S)
        return made[info.uuid]

    return made, factory


async def tv_controller(conn, clock, factory, selected_fake):
    ctrl = await make_controller(conn, clock, selected_fake)
    ctrl.device_factory = factory
    return ctrl


def set_tv(conn, profile_id, uuid):
    conn.execute("UPDATE profile SET cast_device_uuid = ? WHERE id = ?", (uuid, profile_id))


async def test_profile_without_a_tv_plays_on_the_current_device(conn, clock, episodes, kids, tvs):  # PB-6
    made, factory = tvs
    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, factory, living)
    await play_for(ctrl, living, episodes[0], [2])
    assert made == {} and ctrl.device is living and play_calls(living)


async def test_profile_tv_switches_device_and_plays_there(conn, clock, episodes, kids, tvs):  # PB-6
    made, factory = tvs
    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, factory, living)
    set_tv(conn, 2, BEDROOM.uuid)
    await ctrl.play(episodes[0], [2])
    bedroom = made[BEDROOM.uuid]
    assert ctrl.device is bedroom
    assert play_calls(bedroom) and not play_calls(living)
    assert ctrl.state()["device"]["name"] == "Bedroom TV"
    await ctrl.stop_service()


async def test_switching_tv_stops_the_current_session_first(conn, clock, episodes, kids, tvs):  # PB-6, A-5
    made, factory = tvs
    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, factory, living)
    await play_for(ctrl, living, episodes[0], [1])
    first_session = sessions(conn)[0]["id"]
    set_tv(conn, 2, BEDROOM.uuid)
    await ctrl.play(episodes[1], [2])
    assert ("stop",) in living.calls
    old = conn.execute("SELECT end_reason FROM watch_session WHERE id = ?", (first_session,)).fetchone()
    assert old["end_reason"] == EndReason.STOPPED
    assert ctrl.current.episode.id == episodes[1] and ctrl.current.profile_ids == [2]
    await ctrl.stop_service()


async def test_same_tv_is_not_swapped(conn, clock, episodes, kids, tvs):  # PB-6
    made, factory = tvs
    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, factory, living)
    set_tv(conn, 2, LIVING.uuid)
    await play_for(ctrl, living, episodes[0], [2])
    await play_for(ctrl, living, episodes[1], [2])
    assert made == {} and ctrl.device is living


async def test_profile_without_tv_falls_back_to_the_global_device(conn, clock, episodes, kids, tvs):  # PB-6
    made, factory = tvs
    bedroom = FakeCastDevice(clock, info=BEDROOM, default_duration_s=EPISODE_S)  # attached, but not the global one
    ctrl = await tv_controller(conn, clock, factory, bedroom)
    await ctrl.play(episodes[0], [1])  # profile 1 has no TV: the global selected one (living room) is the target
    assert ctrl.device is made[LIVING.uuid]
    await ctrl.stop_service()


async def test_group_uses_the_first_selected_profiles_tv(conn, clock, episodes, kids, tvs):  # PB-6
    made, factory = tvs
    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, factory, living)
    set_tv(conn, 3, BEDROOM.uuid)
    await ctrl.play(episodes[0], [3, 1])  # 3 was picked first
    assert ctrl.device is made[BEDROOM.uuid]
    assert ctrl.current.profile_ids == [1, 3]
    await ctrl.stop_service()


async def test_refused_pick_does_not_switch_tv(conn, clock, episodes, kids, tvs):  # PB-6, PR-4
    made, factory = tvs
    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, factory, living)
    set_tv(conn, 2, BEDROOM.uuid)
    await ctrl.override("block", profile_ids=[2])
    with pytest.raises(PlayRefused):
        await ctrl.play(episodes[0], [2])
    assert made == {} and ctrl.device is living


async def test_unknown_tv_uuid_falls_back_to_the_global_device(conn, clock, episodes, kids, tvs):  # PB-6
    made, factory = tvs
    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, factory, living)
    set_tv(conn, 2, "gone")
    await play_for(ctrl, living, episodes[0], [2])
    assert made == {} and ctrl.device is living


async def test_unreachable_tv_fails_the_pick(conn, clock, episodes, kids, tvs, monkeypatch):  # PB-6
    made, factory = tvs
    monkeypatch.setattr("tellybox.cast.controller.CONNECT_WAIT_S", 0.05)

    def dead(info):
        dev = factory(info)

        async def never(timeout=15.0):
            await asyncio.sleep(3600)

        dev.connect = never
        return dev

    living = FakeCastDevice(clock, info=LIVING, default_duration_s=EPISODE_S)
    ctrl = await tv_controller(conn, clock, dead, living)
    set_tv(conn, 2, BEDROOM.uuid)
    with pytest.raises(CastCommandError):
        await ctrl.play(episodes[0], [2])
    assert ctrl.current is None
    await ctrl.stop_service()


async def test_no_device_at_all_is_still_no_device(conn, clock, episodes, kids):
    ctrl = CastController(conn, clock=clock, tz=TZ, media_base_url="http://tv.test:8080", secret=b"s")
    await ctrl.start(run_loops=False)
    with pytest.raises(NoDevice):
        await ctrl.play(episodes[0], [1])


async def test_profile_tv_lets_a_deviceless_controller_play(conn, clock, episodes, kids, tvs):  # PB-6
    made, factory = tvs
    ctrl = CastController(conn, clock=clock, tz=TZ, media_base_url="http://tv.test:8080", secret=b"s",
                          device=None, device_factory=factory)
    await ctrl.start(run_loops=False)
    set_tv(conn, 2, BEDROOM.uuid)
    await ctrl.play(episodes[0], [2])
    assert ctrl.device is made[BEDROOM.uuid] and ctrl.current is not None
    await ctrl.stop_service()
