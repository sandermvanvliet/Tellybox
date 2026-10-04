"""PB-8: several concurrent playbacks (one TV, any number of devices)."""

import json
from datetime import timedelta

import pytest

from tellybox.timer import TV, Action, Activity, CountingMode, TimeUpReason

from timer_helpers import MIN, START, policy

PLAYING, PAUSED, STOPPED = Activity.PLAYING, Activity.PAUSED, Activity.STOPPED
DEV = "device:a"


def two_playbacks(make_timer, clock, p1=None, p2=None):
    timer = make_timer(p1 or policy(1), p2 or policy(2))
    timer.on_pick(clock.now(), [1], TV)
    timer.set_activity(clock.now(), PLAYING, TV)
    timer.on_pick(clock.now(), [2], DEV)
    timer.set_activity(clock.now(), PLAYING, DEV)
    return timer


def test_two_playbacks_both_accrue(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    clock.advance(minutes=10)
    timer.tick(clock.now())
    assert timer.usage(1).used_s == timer.usage(2).used_s == 10 * MIN
    assert timer.watchers == {1} and timer.watchers_of(DEV) == {2}
    assert timer.activity_of(DEV) is PLAYING and timer.keys() == {TV, DEV}


def test_pause_on_one_playback_does_not_stop_the_other(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    clock.advance(minutes=5)
    timer.set_activity(clock.now(), PAUSED, DEV)
    clock.advance(minutes=5)
    timer.tick(clock.now())
    assert (timer.usage(1).used_s, timer.usage(2).used_s) == (10 * MIN, 5 * MIN)
    assert timer.activity is PLAYING


def test_counting_mode_per_profile_across_playbacks(make_timer, clock):
    timer = two_playbacks(make_timer, clock, policy(1), policy(2, mode=CountingMode.WALL_CLOCK))
    clock.advance(minutes=5)
    timer.set_activity(clock.now(), PAUSED, TV)
    timer.set_activity(clock.now(), PAUSED, DEV)
    clock.advance(minutes=5)
    timer.tick(clock.now())
    assert (timer.usage(1).used_s, timer.usage(2).used_s) == (5 * MIN, 10 * MIN)


def test_exhaustion_only_decides_for_its_own_playback(make_timer, clock):
    timer = two_playbacks(make_timer, clock, policy(1), policy(2, allowance_min=20))
    clock.advance(minutes=25)
    tv, dev = timer.tick(clock.now(), TV), timer.tick(clock.now(), DEV)
    assert tv.action is Action.CONTINUE and tv.reason is None
    assert dev.action is Action.FINISH_THEN_STOP and dev.reason is TimeUpReason.ALLOWANCE
    assert dev.grace_deadline == START + timedelta(minutes=35)
    clock.advance(minutes=11)
    assert timer.tick(clock.now(), DEV).action is Action.STOP_NOW
    assert timer.tick(clock.now(), TV).action is Action.CONTINUE


def test_block_stops_only_that_playback(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    timer.set_blocked(clock.now(), 2, True)
    assert timer.decision(clock.now(), DEV).action is Action.STOP_NOW
    assert timer.decision(clock.now(), TV).action is Action.CONTINUE


def test_grace_depends_on_the_profiles_own_playback(make_timer, clock):
    timer = two_playbacks(make_timer, clock, policy(1), policy(2, allowance_min=20))
    timer.set_activity(clock.now(), STOPPED, DEV)  # device stopped, TV still playing
    clock.advance(minutes=1)
    timer.tick(clock.now())
    timer.add_extra(clock.now(), 2, -20 * MIN)  # profile 2 runs out while its playback is stopped
    d = timer.decision(clock.now(), DEV)
    assert d.reason is TimeUpReason.ALLOWANCE and d.action is Action.STOP_NOW  # no grace


def test_profile_moving_between_playbacks_leaves_the_first(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    clock.advance(minutes=5)
    timer.on_pick(clock.now(), [1], DEV)  # profile 1 picks on the device: leaves the TV
    assert timer.moved_from() == {TV: frozenset({1})}
    assert timer.watchers == frozenset()
    assert timer.watchers_of(DEV) == {1}  # profile 2 was replaced by the new pick
    assert timer.moved_from().get(DEV) is None
    clock.advance(minutes=5)
    timer.tick(clock.now())
    assert timer.usage(1).used_s == 10 * MIN
    assert timer.usage(2).used_s == 5 * MIN  # 2 stopped watching at the pick


def test_moving_starts_session_break_for_removed_watchers(make_timer, clock):
    timer = make_timer(policy(1), policy(2), policy(3))
    timer.on_pick(clock.now(), [1, 2], TV)
    timer.set_activity(clock.now(), PLAYING, TV)
    clock.advance(minutes=5)
    timer.on_pick(clock.now(), [2], DEV)  # 2 moves; 1 stays on the TV
    assert timer.moved_from() == {TV: frozenset({2})}
    assert timer.watchers == {1}
    timer.set_activity(clock.now(), PLAYING, DEV)
    clock.advance(minutes=20)
    status = timer.profile_status(clock.now(), 2)
    assert status.watching and status.session_elapsed_s == 25 * MIN  # session continued


def test_end_playback_starts_session_break(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    clock.advance(minutes=5)
    timer.end_playback(clock.now(), DEV)
    assert DEV not in timer.keys() and timer.watchers_of(DEV) == frozenset()
    assert not timer.profile_status(clock.now(), 2).watching
    clock.advance(minutes=14)
    assert timer.profile_status(clock.now(), 2).session_elapsed_s == 19 * MIN
    clock.advance(minutes=2)  # past the 15-minute break (WT-3)
    assert timer.profile_status(clock.now(), 2).session_elapsed_s is None
    assert timer.usage(2).used_s == 5 * MIN
    with pytest.raises(ValueError):
        timer.end_playback(clock.now(), TV)


def test_tv_pick_while_everyone_implied_materialises(make_timer, clock):
    timer = make_timer(policy(1), policy(2))
    timer.on_pick(clock.now(), [2], DEV)  # no TV pick yet: TV implicitly has everyone
    assert timer.moved_from() == {TV: frozenset({2})}
    assert timer.watchers == {1}


def test_failed_pick_reports_nothing_moved(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    timer.set_blocked(clock.now(), 1, True)
    timer.on_pick(clock.now(), [1], DEV)
    assert timer.moved_from() == {} and timer.watchers == {1}


def test_counted_seconds_per_key(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    clock.advance(minutes=10)
    timer.set_activity(clock.now(), PAUSED, DEV)
    clock.advance(minutes=5)
    timer.tick(clock.now())
    assert timer.pop_counted_s(DEV) == 10 * MIN
    assert timer.pop_counted_s(DEV) == 0.0
    assert timer.pop_counted_s() == 15 * MIN  # TV's share is what is left of the total
    assert timer.pop_counted_s() == 0.0


def test_total_counted_seconds_sums_playbacks(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    clock.advance(minutes=10)
    timer.tick(clock.now())
    assert timer.pop_counted_s() == 20 * MIN


def test_snapshot_round_trip_keeps_sessions_of_device_watchers(make_timer, clock):
    timer = two_playbacks(make_timer, clock)
    clock.advance(minutes=10)
    timer.tick(clock.now())
    snap = json.loads(json.dumps(timer.snapshot()))
    assert snap["playbacks"] == {TV: {"watchers": [1]}, DEV: {"watchers": [2]}}
    clock.advance(minutes=1)
    restored = make_timer(policy(1), policy(2), usages=[timer.usage(1), timer.usage(2)], snapshot=snap)
    assert restored.keys() == {TV}  # devices must reconnect
    assert restored.watchers == {1} and restored.activity is STOPPED
    assert restored.profile_status(clock.now(), 2).session_elapsed_s == 11 * MIN  # kept, in its break
    clock.advance(minutes=20)
    assert restored.profile_status(clock.now(), 2).session_elapsed_s is None


def test_snapshot_without_playbacks_map_restores_tv_watchers(make_timer, clock):
    timer = make_timer(policy(1), policy(2))
    timer.on_pick(clock.now(), [2])
    snap = json.loads(json.dumps(timer.snapshot()))
    del snap["playbacks"]  # as written before PB-8
    restored = make_timer(policy(1), policy(2), snapshot=snap)
    assert restored.watchers == {2}


def test_snapshot_playbacks_map_wins_for_tv(make_timer, clock):
    timer = make_timer(policy(1), policy(2))
    timer.on_pick(clock.now(), [1])
    snap = json.loads(json.dumps(timer.snapshot()))
    snap["playbacks"][TV]["watchers"] = [2]
    assert make_timer(policy(1), policy(2), snapshot=snap).watchers == {2}


def test_policy_refresh_does_not_put_device_watchers_back_on_tv(make_timer, clock):
    """The last TV watcher moves to a device; a policy refresh resets the empty TV set to "everyone".
    The device's profile must still pay once (PB-8)."""
    p1, p2 = policy(1), policy(2)
    timer = make_timer(p1, p2)
    timer.on_pick(clock.now(), [1], TV)
    timer.on_pick(clock.now(), [1], DEV)
    timer.set_activity(clock.now(), PLAYING, DEV)
    timer.set_policies(timer._settings, [p1, p2], clock.now())
    timer.set_activity(clock.now(), PLAYING, TV)
    clock.advance(minutes=10)
    timer.tick(clock.now())
    assert timer.usage(1).used_s == 10 * MIN
    assert 1 not in timer.watchers and timer.watchers_of(DEV) == {1}
