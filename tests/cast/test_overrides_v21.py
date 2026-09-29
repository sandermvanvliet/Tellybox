"""Admin API additions to the cast service (step 9, HA-2..HA-4, HA-7): override targets, clear, source, new state fields."""

from datetime import datetime, timedelta

import pytest

from tellybox.cast.controller import PlayRefused

from test_controller import (  # noqa: F401  (fixtures)
    clock,
    conn,
    episodes,
    fake,
    make_controller,
    pump,
    run_for,
)
from test_controller_profiles import kids, play_for  # noqa: F401


def flags(ctrl) -> dict[int, tuple[bool, bool]]:
    return {p["profile_id"]: (p["unlimited"], p["blocked"]) for p in ctrl.state()["timer"]["profiles"]}


async def test_override_for_a_subset_leaves_the_others(conn, clock, fake, episodes, kids):  # HA-3
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.override("unlimited", profile_ids=[1, 3])
    await ctrl.override("block", profile_ids=[2])
    assert flags(ctrl) == {1: (True, False), 2: (False, True), 3: (True, False)}
    await ctrl.override("extra_minutes", 5, profile_ids=[3])
    extra = {p["profile_id"]: p["extra_s"] for p in ctrl.state()["timer"]["profiles"]}
    assert extra == {1: 0, 2: 0, 3: 300}
    rows = conn.execute("SELECT profile_id FROM override_log WHERE kind = 'extra_minutes'").fetchall()
    assert [r[0] for r in rows] == [3]


async def test_clear_lifts_unlimited_and_block_and_logs_per_profile(conn, clock, fake, episodes, kids):  # HA-3
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.override("unlimited", profile_ids=[1, 2])
    await ctrl.override("block", profile_ids=[2, 3])
    await ctrl.override("extra_minutes", 10, profile_ids=[1])
    await ctrl.override("clear", profile_ids=[1, 2])
    assert flags(ctrl) == {1: (False, False), 2: (False, False), 3: (False, True)}
    assert next(p for p in ctrl.state()["timer"]["profiles"] if p["profile_id"] == 1)["extra_s"] == 600  # stays
    rows = conn.execute("SELECT profile_id, value FROM override_log WHERE kind = 'clear' ORDER BY profile_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, None), (2, None)]


async def test_clear_without_targets_is_every_profile(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.override("block")
    await ctrl.override("clear")
    assert set(flags(ctrl).values()) == {(False, False)}
    assert conn.execute("SELECT COUNT(*) FROM override_log WHERE kind = 'clear'").fetchone()[0] == 3


async def test_clear_of_a_blocked_profile_allows_playing_again(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.override("block", profile_ids=[2])
    with pytest.raises(PlayRefused):
        await ctrl.play(episodes[0], [2])
    await ctrl.override("clear", profile_ids=[2])
    await ctrl.play(episodes[0], [2])
    assert ctrl.current is not None


async def test_source_is_logged(conn, clock, fake, episodes, kids):  # HA-7
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.override("block", profile_ids=[1, 2], source="Home Assistant")
    await ctrl.override("unlimited", profile_ids=[3])
    rows = conn.execute("SELECT kind, source FROM override_log ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("block", "Home Assistant"), ("block", "Home Assistant"), ("unlimited", None)]


async def test_stop_now_ignores_profiles(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    await play_for(ctrl, fake, episodes[0], [1])
    await ctrl.override("stop_now", profile_ids=[2], source="tok")
    await pump(ctrl, fake)
    assert ctrl.current is None
    assert flags(ctrl) == {1: (False, False), 2: (False, False), 3: (False, False)}


async def test_unknown_profile_ids_are_refused_without_effect(conn, clock, fake, episodes, kids):
    ctrl = await make_controller(conn, clock, fake)
    with pytest.raises(ValueError):
        await ctrl.override("block", profile_ids=[1, 99])
    assert flags(ctrl)[1] == (False, False)
    assert conn.execute("SELECT COUNT(*) FROM override_log").fetchone()[0] == 0


async def test_state_next_reset_and_session_elapsed(conn, clock, fake, episodes, kids):  # HA-2, WT-1, WT-3
    ctrl = await make_controller(conn, clock, fake)
    timer = ctrl.state()["timer"]
    reset = datetime.fromisoformat(timer["next_reset"])
    assert reset.tzinfo is not None and reset > clock.now()
    local = reset.astimezone(ctrl.tz)
    assert (local.hour, local.minute) == (4, 0)
    assert all(p["session_elapsed_s"] is None for p in timer["profiles"])
    await play_for(ctrl, fake, episodes[0], [2])
    await run_for(ctrl, fake, clock, 60)
    by_id = {p["profile_id"]: p for p in ctrl.state()["timer"]["profiles"]}
    assert by_id[2]["session_elapsed_s"] == pytest.approx(60, abs=2)
    assert by_id[1]["session_elapsed_s"] is None
    clock.advance((reset - clock.now()).total_seconds() + 1)  # across the reset
    await ctrl.tick()
    assert datetime.fromisoformat(ctrl.state()["timer"]["next_reset"]) - reset == timedelta(days=1)
