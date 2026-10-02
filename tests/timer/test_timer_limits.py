"""Per-profile limits: inherit, custom, or unlimited (A-23)."""

from datetime import timedelta

import pytest

from tellybox.timer import Action, Activity, CountingMode, TimeUpReason

from timer_helpers import MIN, START, policy

PLAYING = Activity.PLAYING


def test_unlimited_allowance_never_exhausts(make_timer, clock):
    """An unlimited allowance never hits the ALLOWANCE exhaustion."""
    timer = make_timer(policy(1, allowance_min=None, max_session_min=None))
    timer.on_pick(clock.now())
    timer.set_activity(clock.now(), PLAYING)

    # Play for many hours
    clock.advance(hours=10)
    decision = timer.tick(clock.now())

    assert decision.action is Action.CONTINUE
    assert decision.reason is None
    assert decision.can_start
    assert decision.remaining_s is None


def test_unlimited_max_session_never_ends(make_timer, clock):
    """An unlimited max_session never hits the SESSION_MAX exhaustion."""
    timer = make_timer(policy(1, allowance_min=None, max_session_min=None))
    timer.on_pick(clock.now())
    timer.set_activity(clock.now(), PLAYING)

    # Play for many hours
    clock.advance(hours=5)
    decision = timer.tick(clock.now())

    assert decision.action is Action.CONTINUE
    assert decision.reason is None
    assert decision.remaining_s is None


def test_mixed_group_limited_and_unlimited_stops_when_limited_runs_out(make_timer, clock):
    """When one profile in a group has limited allowance and another has unlimited,
    the group stops when the limited one runs out."""
    timer = make_timer(
        policy(1, allowance_min=30),  # limited
        policy(2, allowance_min=None),  # unlimited
    )

    # Both start watching together
    timer.on_pick(clock.now(), [1, 2])
    timer.set_activity(clock.now(), PLAYING)

    # Play for 31 minutes
    clock.advance(minutes=31)
    decision = timer.tick(clock.now())

    # The limited profile (1) has exhausted its allowance
    assert decision.reason is TimeUpReason.ALLOWANCE
    assert decision.action is Action.FINISH_THEN_STOP
    # The minimum remaining is 0 (from profile 1)
    assert decision.remaining_s == 0


def test_set_policies_unlimited_to_limited_exhausts(make_timer, clock):
    """When switching a profile from unlimited to limited mid-playback,
    if the profile has already played more than the new limit, it should be exhausted."""
    timer = make_timer(policy(1, allowance_min=None))
    timer.on_pick(clock.now())
    timer.set_activity(clock.now(), PLAYING)

    # Play for 60 minutes (unlimited, so no exhaustion)
    clock.advance(minutes=60)
    decision = timer.tick(clock.now())
    assert decision.reason is None

    # Now set a 45-minute limit
    from tellybox.timer import ProfilePolicy, TimerSettings
    from datetime import time
    from zoneinfo import ZoneInfo

    new_policy = policy(1, allowance_min=45)
    new_settings = TimerSettings(reset_time=time(4, 0), tz=ZoneInfo("Europe/Amsterdam"))
    timer.set_policies(new_settings, [new_policy], clock.now())

    decision = timer.tick(clock.now())
    # Should be exhausted because 60 minutes > 45 minute limit
    assert decision.reason is TimeUpReason.ALLOWANCE
    # Will have grace because media is loaded (PLAYING)
    assert decision.action is Action.FINISH_THEN_STOP
    assert decision.grace_deadline is not None


def test_set_policies_limited_to_unlimited_lifts_exhaustion(make_timer, clock):
    """When switching a profile from limited to unlimited mid-playback,
    an exhaustion should be lifted."""
    timer = make_timer(policy(1, allowance_min=30))
    timer.on_pick(clock.now())
    timer.set_activity(clock.now(), PLAYING)

    # Play for 35 minutes (exceeds 30 minute limit)
    clock.advance(minutes=35)
    decision = timer.tick(clock.now())
    assert decision.reason is TimeUpReason.ALLOWANCE

    # Now set unlimited
    from tellybox.timer import ProfilePolicy, TimerSettings
    from datetime import time
    from zoneinfo import ZoneInfo

    new_policy = policy(1, allowance_min=None)
    new_settings = TimerSettings(reset_time=time(4, 0), tz=ZoneInfo("Europe/Amsterdam"))
    timer.set_policies(new_settings, [new_policy], clock.now())

    decision = timer.tick(clock.now())
    # Exhaustion should be lifted
    assert decision.reason is None
    assert decision.can_start


def test_unlimited_max_session_mixed_group(make_timer, clock):
    """When one profile in a group has unlimited max_session and another has limited,
    the group stops when the limited one hits the max session length."""
    timer = make_timer(
        policy(1, allowance_min=120, max_session_min=60),  # limited session
        policy(2, allowance_min=120, max_session_min=None),  # unlimited session
    )

    # Both start watching together
    timer.on_pick(clock.now(), [1, 2])
    timer.set_activity(clock.now(), PLAYING)

    # Play for 61 minutes (exceeds profile 1's 60-minute session max)
    clock.advance(minutes=61)
    decision = timer.tick(clock.now())

    # Profile 1 should hit SESSION_MAX exhaustion
    assert decision.reason is TimeUpReason.SESSION_MAX
    assert decision.action is Action.FINISH_THEN_STOP


def test_unlimited_allowance_with_limited_session(make_timer, clock):
    """A profile can have unlimited allowance but limited max_session."""
    timer = make_timer(policy(1, allowance_min=None, max_session_min=60))
    timer.on_pick(clock.now())
    timer.set_activity(clock.now(), PLAYING)

    # Play for 61 minutes
    clock.advance(minutes=61)
    decision = timer.tick(clock.now())

    # Should hit session max, not allowance
    assert decision.reason is TimeUpReason.SESSION_MAX
    assert decision.remaining_s is None  # allowance is unlimited
