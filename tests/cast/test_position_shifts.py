"""SB-3: saved positions follow a replaced file (store function and controller)."""

from datetime import UTC, datetime

import pytest

from tellybox import library, sponsorblock, store
from tellybox.cast.fake import FakeCastDevice
from tellybox.clock import FakeClock
from tellybox.db import open_db, to_db
from tests.cast.test_controller import EPISODE_S, START, make_controller, play, play_calls

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
CUT = [sponsorblock.Segment("sponsor", 100.0, 130.0)]
CUT2 = [sponsorblock.Segment("sponsor", 100.0, 130.0), sponsorblock.Segment("selfpromo", 300.0, 320.0)]


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "tellybox.db")


@pytest.fixture
def episodes(conn):
    show_id = library.create_show(conn, "Dev show", now=NOW)
    return [
        library.add_episode(conn, show_id, f"Episode {i}", f"dev/ep{i}.mp4", now=NOW, duration_s=EPISODE_S) for i in (1, 2)
    ]


def add_profile(conn, name="Noor") -> int:
    return conn.execute("INSERT INTO profile (name, created_at) VALUES (?, ?)", (name, to_db(NOW))).lastrowid


def set_position(conn, profile_id, episode_id, position_s, finished=False):
    store.save_position(conn, [profile_id], episode_id, position_s, finished, NOW)


def queue_shift(conn, episode_id, old, new):
    conn.execute(
        "INSERT INTO position_shift (episode_id, old_cuts_json, new_cuts_json, created_at) VALUES (?, ?, ?, ?)",
        (episode_id, sponsorblock.dumps(old), sponsorblock.dumps(new), to_db(NOW)),
    )


def position(conn, profile_id, episode_id):
    return store.get_position(conn, profile_id, episode_id)


def test_empty_queue_applies_nothing(conn):
    assert store.apply_position_shifts(conn, NOW) == 0


def test_positions_of_every_profile_move_and_the_row_is_deleted(conn, episodes):
    a, b = 1, add_profile(conn)
    set_position(conn, a, episodes[0], 200.0)
    set_position(conn, b, episodes[0], 50.0, finished=False)
    queue_shift(conn, episodes[0], [], CUT)

    later = datetime(2026, 9, 30, 13, 0, tzinfo=UTC)
    assert store.apply_position_shifts(conn, later) == 1

    assert position(conn, a, episodes[0]) == (170.0, False)
    assert position(conn, b, episodes[0]) == (50.0, False)  # before the cut: unchanged
    assert conn.execute("SELECT COUNT(*) FROM position_shift").fetchone()[0] == 0
    row = conn.execute("SELECT updated_at FROM playback_position WHERE profile_id = ?", (a,)).fetchone()
    assert row["updated_at"] != to_db(later)  # keeps its place in continue watching (PB-4)


def test_position_inside_a_new_cut_lands_where_the_cut_was(conn, episodes):
    set_position(conn, 1, episodes[0], 110.0)
    queue_shift(conn, episodes[0], [], CUT)
    store.apply_position_shifts(conn, NOW)
    assert position(conn, 1, episodes[0]) == (100.0, False)


def test_undone_cut_moves_the_position_forward(conn, episodes):
    set_position(conn, 1, episodes[0], 170.0)
    queue_shift(conn, episodes[0], CUT, [])
    store.apply_position_shifts(conn, NOW)
    assert position(conn, 1, episodes[0]) == (200.0, False)


def test_finished_flag_is_kept(conn, episodes):
    set_position(conn, 1, episodes[0], 590.0, finished=True)
    queue_shift(conn, episodes[0], [], CUT)
    store.apply_position_shifts(conn, NOW)
    assert position(conn, 1, episodes[0]) == (560.0, True)


def test_position_is_clamped_to_the_current_duration(conn, episodes):
    set_position(conn, 1, episodes[0], 590.0)
    conn.execute("UPDATE episode SET duration_s = 300 WHERE id = ?", (episodes[0],))
    queue_shift(conn, episodes[0], [], CUT)
    store.apply_position_shifts(conn, NOW)
    assert position(conn, 1, episodes[0]) == (300.0, False)


def test_queued_shifts_apply_in_order(conn, episodes):
    set_position(conn, 1, episodes[0], 400.0)
    queue_shift(conn, episodes[0], [], CUT)  # 400 -> 370
    queue_shift(conn, episodes[0], CUT, CUT2)  # 370 -> original 400 -> 350
    queue_shift(conn, episodes[0], CUT2, [])  # 350 -> 400
    assert store.apply_position_shifts(conn, NOW) == 3
    assert position(conn, 1, episodes[0]) == (400.0, False)


def test_other_episodes_are_untouched(conn, episodes):
    set_position(conn, 1, episodes[0], 200.0)
    set_position(conn, 1, episodes[1], 200.0)
    queue_shift(conn, episodes[0], [], CUT)
    store.apply_position_shifts(conn, NOW)
    assert position(conn, 1, episodes[1]) == (200.0, False)


def test_shift_without_saved_positions_is_just_deleted(conn, episodes):
    queue_shift(conn, episodes[0], [], CUT)
    assert store.apply_position_shifts(conn, NOW) == 1
    assert conn.execute("SELECT COUNT(*) FROM playback_position").fetchone()[0] == 0


# --------------------------------------------------------------------------- controller


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def fake(clock):
    return FakeCastDevice(clock, default_duration_s=EPISODE_S)


async def test_pick_resumes_at_the_remapped_position(conn, clock, fake, episodes):
    set_position(conn, 1, episodes[0], 200.0)
    queue_shift(conn, episodes[0], [], CUT)
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])

    (_, _, start_s), = play_calls(fake)
    assert start_s == 170.0
    assert conn.execute("SELECT COUNT(*) FROM position_shift").fetchone()[0] == 0


async def test_tick_applies_queued_shifts(conn, clock, fake, episodes):
    set_position(conn, 1, episodes[1], 200.0)
    ctrl = await make_controller(conn, clock, fake)
    queue_shift(conn, episodes[1], [], CUT)
    clock.advance(1)
    await ctrl.tick()
    assert position(conn, 1, episodes[1]) == (170.0, False)
    assert conn.execute("SELECT COUNT(*) FROM position_shift").fetchone()[0] == 0
