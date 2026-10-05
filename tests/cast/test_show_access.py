"""Show access in the cast service (PR-5..PR-8, A-37): refused picks, autoplay and revocation, with a
fake clock and a fake Chromecast."""

import pytest

from tellybox import show_access, store
from tellybox.cast.controller import EndReason, ShowNotAllowed
from tellybox.db import to_db

from test_controller import (  # noqa: F401  (fixtures)
    EPISODE_S,
    clock,
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
from test_controller_devices import DEV, beat, watch  # noqa: F401

pytestmark = pytest.mark.strict_access


@pytest.fixture
def show_id(conn, episodes):
    return conn.execute("SELECT show_id FROM episode WHERE id = ?", (episodes[0],)).fetchone()[0]


@pytest.fixture
def kids(conn, clock):
    for name in ("B", "C"):
        conn.execute("INSERT INTO profile (name, created_at) VALUES (?, ?)", (name, to_db(clock.now())))
    return [1, 2, 3]


async def test_tv_pick_of_a_hidden_show_is_refused_and_changes_nothing(conn, clock, fake, episodes, show_id):  # PR-7
    other = conn.execute("INSERT INTO show (name, created_at) VALUES ('Other', 'x')").lastrowid
    oe = conn.execute("INSERT INTO episode (show_id, title, file_path, sort_order, created_at) "
                      "VALUES (?, 'o', 'o.mp4', 1, 'x')", (other,)).lastrowid
    show_access.grant(conn, 1, show_id)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    with pytest.raises(ShowNotAllowed):
        await ctrl.play(oe, [1])
    assert ctrl.current.episode.id == episodes[0]  # current playback undisturbed
    assert len(play_calls(fake)) == 1


async def test_a_group_pick_needs_every_member_to_have_the_show(conn, clock, fake, episodes, show_id, kids):  # PR-6
    show_access.grant(conn, 2, show_id)
    ctrl = await make_controller(conn, clock, fake)
    with pytest.raises(ShowNotAllowed):
        await ctrl.play(episodes[0], [2, 3])
    assert ctrl.current is None and not play_calls(fake)
    show_access.grant(conn, 3, show_id)
    await ctrl.play(episodes[0], [2, 3])
    assert ctrl.current.profile_ids == [2, 3]


async def test_device_pick_of_a_hidden_show_is_refused(conn, clock, fake, episodes, show_id, kids):  # PR-7, PB-7
    show_access.grant(conn, 2, show_id)
    ctrl = await make_controller(conn, clock, fake)
    with pytest.raises(ShowNotAllowed):
        await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    assert not ctrl.device_sessions and not sessions(conn)


async def test_revoke_mid_episode_finishes_it_and_stops_autoplay_on_tv(conn, clock, fake, episodes, show_id):  # PR-8
    show_access.grant(conn, 1, show_id)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 60)
    show_access.revoke(conn, 1, show_id)
    await run_for(ctrl, fake, clock, 30)
    assert ctrl.current is not None and ctrl.current.episode.id == episodes[0]  # no mid-episode cut-off
    assert ctrl._up_next() is None  # CR-5: no up-next thumbnail either
    await run_for(ctrl, fake, clock, EPISODE_S)
    assert len(play_calls(fake)) == 1  # no autoplay
    assert ctrl.current is None
    assert sessions(conn)[0]["end_reason"] == EndReason.FINISHED


async def test_autoplay_stops_for_the_group_when_one_member_lost_access(conn, clock, fake, episodes, show_id, kids):  # PR-8
    show_access.grant(conn, 2, show_id)
    show_access.grant(conn, 3, show_id)
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.play(episodes[0], [2, 3])
    await pump(ctrl, fake)
    show_access.revoke(conn, 3, show_id)
    await run_for(ctrl, fake, clock, EPISODE_S + 5)
    assert len(play_calls(fake)) == 1 and ctrl.current is None


async def test_autoplay_continues_while_access_holds(conn, clock, fake, episodes, show_id):  # PB-3
    show_access.grant(conn, 1, show_id)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, EPISODE_S + 5)
    assert ctrl.current.episode.id == episodes[1]


async def test_revoke_mid_episode_on_device_stops_autoplay_next(conn, clock, fake, episodes, show_id):  # PR-8
    show_access.grant(conn, 1, show_id)
    ctrl = await make_controller(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    await watch(ctrl, clock, DEV, 330)
    show_access.revoke(conn, 1, show_id)
    answer = await beat(ctrl, clock, DEV, "playing", 340)
    assert answer["action"] == "continue"  # the episode in progress finishes
    answer = await beat(ctrl, clock, DEV, "ended", EPISODE_S)
    assert (answer["action"], answer["reason"]) == ("stop", "finished")
    assert not ctrl.device_sessions


async def test_revoke_then_regrant_restores_resume(conn, clock, fake, episodes, show_id):  # PR-8
    show_access.grant(conn, 1, show_id)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 120)
    await ctrl.stop()
    saved = store.get_position(conn, 1, episodes[0])
    assert saved[0] > 60
    show_access.revoke(conn, 1, show_id)
    assert store.get_position(conn, 1, episodes[0]) == saved  # kept
    show_access.grant(conn, 1, show_id)
    assert store.get_position(conn, 1, episodes[0]) == saved
