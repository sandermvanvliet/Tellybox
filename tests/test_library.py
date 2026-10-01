"""Show/episode queries used by the cast controller and the dev CLI."""

from datetime import UTC, datetime

import pytest

from tellybox import db, library

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    c = db.open_db(":memory:")
    yield c
    c.close()


def test_create_and_get_show(conn):
    sid = library.create_show(conn, "Bluey", now=NOW)
    show = library.get_show(conn, sid)
    assert show == library.Show(id=sid, name="Bluey", autoplay=True, hidden=False, sort_order=0)
    row = conn.execute("SELECT created_at FROM show WHERE id = ?", (sid,)).fetchone()
    assert row["created_at"] == db.to_db(NOW)


def test_create_show_without_autoplay(conn):
    sid = library.create_show(conn, "Pingu", now=NOW, autoplay=False)
    assert library.get_show(conn, sid).autoplay is False


def test_get_missing(conn):
    assert library.get_show(conn, 999) is None
    assert library.get_episode(conn, 999) is None


def test_add_episode_defaults_sort_order_to_end(conn):
    sid = library.create_show(conn, "Bluey", now=NOW)
    e1 = library.add_episode(conn, sid, "One", "bluey/1.mp4", now=NOW, duration_s=420.5)
    e2 = library.add_episode(conn, sid, "Two", "bluey/2.mp4", now=NOW)
    e10 = library.add_episode(conn, sid, "Ten", "bluey/10.mp4", now=NOW, sort_order=10)
    e11 = library.add_episode(conn, sid, "Eleven", "bluey/11.mp4", now=NOW)
    ep1 = library.get_episode(conn, e1)
    assert ep1 == library.Episode(
        id=e1, show_id=sid, title="One", file_path="bluey/1.mp4", duration_s=420.5, sort_order=0, hidden=False
    )
    assert library.get_episode(conn, e2).sort_order == 1
    assert library.get_episode(conn, e2).duration_s is None
    assert library.get_episode(conn, e10).sort_order == 10
    assert library.get_episode(conn, e11).sort_order == 11


def test_sort_order_is_per_show(conn):
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    library.add_episode(conn, a, "a1", "a/1.mp4", now=NOW)
    library.add_episode(conn, a, "a2", "a/2.mp4", now=NOW)
    b1 = library.add_episode(conn, b, "b1", "b/1.mp4", now=NOW)
    assert library.get_episode(conn, b1).sort_order == 0


def _hide(conn, episode_id):
    conn.execute("UPDATE episode SET hidden = 1 WHERE id = ?", (episode_id,))


def test_list_episodes_ordered_and_filters_hidden(conn):
    sid = library.create_show(conn, "S", now=NOW)
    e_b = library.add_episode(conn, sid, "b", "s/b.mp4", now=NOW, sort_order=2)
    e_a = library.add_episode(conn, sid, "a", "s/a.mp4", now=NOW, sort_order=1)
    e_c = library.add_episode(conn, sid, "c", "s/c.mp4", now=NOW, sort_order=2)  # tie: by id
    _hide(conn, e_c)
    assert [e.id for e in library.list_episodes(conn, sid)] == [e_a, e_b]
    all_eps = library.list_episodes(conn, sid, include_hidden=True)
    assert [e.id for e in all_eps] == [e_a, e_b, e_c]
    assert all_eps[2].hidden is True


def test_next_episode_skips_hidden_and_stops_at_end(conn):
    # PB-3: autoplay goes to the next visible episode of the same show.
    sid = library.create_show(conn, "S", now=NOW)
    other = library.create_show(conn, "Other", now=NOW)
    e1 = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW)
    e2 = library.add_episode(conn, sid, "2", "s/2.mp4", now=NOW)
    e3 = library.add_episode(conn, sid, "3", "s/3.mp4", now=NOW)
    library.add_episode(conn, other, "x", "o/x.mp4", now=NOW, sort_order=99)
    _hide(conn, e2)
    assert library.next_episode(conn, e1).id == e3
    assert library.next_episode(conn, e2).id == e3  # from a hidden episode still works
    assert library.next_episode(conn, e3) is None
    assert library.next_episode(conn, 999) is None


def test_next_episode_ties_broken_by_id(conn):
    sid = library.create_show(conn, "S", now=NOW)
    e1 = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW, sort_order=5)
    e2 = library.add_episode(conn, sid, "2", "s/2.mp4", now=NOW, sort_order=5)
    e0 = library.add_episode(conn, sid, "0", "s/0.mp4", now=NOW, sort_order=1)
    assert library.next_episode(conn, e0).id == e1
    assert library.next_episode(conn, e1).id == e2
    assert library.next_episode(conn, e2) is None


# --- Step 3: ingest-related fields and admin library management (CI-4, CI-6, LM-1, LM-3) ---


def _source(conn, youtube_id, *, show_id=None, file_path=None, thumbnail_path=None):
    return conn.execute(
        """INSERT INTO source_video (youtube_id, url, title, show_id, file_path, thumbnail_path, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (youtube_id, f"https://youtu.be/{youtube_id}", youtube_id, show_id, file_path, thumbnail_path,
         db.to_db(NOW), db.to_db(NOW)),
    ).lastrowid


def _file(media, rel, size):
    p = media / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_show_channel_and_artwork_fields(conn):
    sid = library.create_show(conn, "Bluey", now=NOW, youtube_channel_id="UC123")
    conn.execute("UPDATE show SET artwork_path = 'bluey/art.jpg' WHERE id = ?", (sid,))
    show = library.get_show(conn, sid)
    assert (show.youtube_channel_id, show.artwork_path) == ("UC123", "bluey/art.jpg")
    assert library.get_show(conn, library.create_show(conn, "Plain", now=NOW)).youtube_channel_id is None


def test_find_show_by_channel_returns_oldest(conn):
    first = library.create_show(conn, "A", now=NOW, youtube_channel_id="UC1")
    library.create_show(conn, "B", now=NOW, youtube_channel_id="UC1")
    assert library.find_show_by_channel(conn, "UC1").id == first
    assert library.find_show_by_channel(conn, "UC2") is None


def test_add_episode_with_source_thumbnail_and_hidden(conn):
    sid = library.create_show(conn, "S", now=NOW)
    src = _source(conn, "abc", show_id=sid)
    eid = library.add_episode(
        conn, sid, "One", "s/1.mp4", now=NOW, source_video_id=src, thumbnail_path="s/1.jpg", hidden=True
    )
    ep = library.get_episode(conn, eid)
    assert (ep.source_video_id, ep.hidden, ep.start_s, ep.end_s) == (src, True, None, None)
    assert conn.execute("SELECT thumbnail_path FROM episode WHERE id = ?", (eid,)).fetchone()[0] == "s/1.jpg"
    conn.execute("UPDATE episode SET start_s = 1.5, end_s = 60 WHERE id = ?", (eid,))
    ep = library.list_episodes(conn, sid, include_hidden=True)[0]
    assert (ep.start_s, ep.end_s) == (1.5, 60.0)


def test_rename_show(conn):
    sid = library.create_show(conn, "Old", now=NOW)
    library.rename_show(conn, sid, "  New  ")
    assert library.get_show(conn, sid).name == "New"
    with pytest.raises(ValueError):
        library.rename_show(conn, sid, "   ")
    with pytest.raises(KeyError):
        library.rename_show(conn, 999, "x")


def test_merge_shows_appends_in_order_and_moves_sources(conn):
    into = library.create_show(conn, "Into", now=NOW)
    frm = library.create_show(conn, "From", now=NOW)
    i1 = library.add_episode(conn, into, "i1", "i/1.mp4", now=NOW, sort_order=3)
    f_b = library.add_episode(conn, frm, "fb", "f/b.mp4", now=NOW, sort_order=5)
    f_a = library.add_episode(conn, frm, "fa", "f/a.mp4", now=NOW, sort_order=1)
    f_c = library.add_episode(conn, frm, "fc", "f/c.mp4", now=NOW, sort_order=5)  # tie: by id
    src = _source(conn, "s1", show_id=frm)
    library.merge_shows(conn, into, frm, now=NOW)
    eps = library.list_episodes(conn, into)
    assert [e.id for e in eps] == [i1, f_a, f_b, f_c]
    assert [e.sort_order for e in eps] == [3, 4, 5, 6]
    assert library.get_show(conn, frm) is None
    assert conn.execute("SELECT show_id FROM source_video WHERE id = ?", (src,)).fetchone()[0] == into
    with pytest.raises(ValueError):
        library.merge_shows(conn, into, into, now=NOW)
    with pytest.raises(KeyError):
        library.merge_shows(conn, into, 999, now=NOW)


def test_merge_into_empty_show(conn):
    into = library.create_show(conn, "Into", now=NOW)
    frm = library.create_show(conn, "From", now=NOW)
    e = library.add_episode(conn, frm, "x", "f/x.mp4", now=NOW, sort_order=7)
    library.merge_shows(conn, into, frm, now=NOW)
    assert library.get_episode(conn, e).sort_order == 0


def test_set_hidden(conn):
    sid = library.create_show(conn, "S", now=NOW)
    eid = library.add_episode(conn, sid, "e", "s/e.mp4", now=NOW)
    library.set_show_hidden(conn, sid, True)
    library.set_episode_hidden(conn, eid, True)
    assert library.get_show(conn, sid).hidden and library.get_episode(conn, eid).hidden
    library.set_show_hidden(conn, sid, False)
    library.set_episode_hidden(conn, eid, False)
    assert not library.get_show(conn, sid).hidden and not library.get_episode(conn, eid).hidden
    with pytest.raises(KeyError):
        library.set_episode_hidden(conn, 999, True)
    with pytest.raises(KeyError):
        library.set_show_hidden(conn, 999, True)


def test_disk_usage_counts_shared_files_once(conn, tmp_path):
    media = tmp_path / "media"
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    empty = library.create_show(conn, "Empty", now=NOW)
    _file(media, "a/1.mp4", 1000)
    _file(media, "a/1.jpg", 10)
    _file(media, "a/art.jpg", 5)
    _file(media, "b/2.mp4", 300)
    _file(media, "src/orphan.mp4", 7)
    conn.execute("UPDATE show SET artwork_path = 'a/art.jpg' WHERE id = ?", (a,))
    src = _source(conn, "v1", show_id=a, file_path="a/1.mp4", thumbnail_path="a/1.jpg")  # shared with episode
    library.add_episode(conn, a, "1", "a/1.mp4", now=NOW, source_video_id=src, thumbnail_path="a/1.jpg")
    library.add_episode(conn, a, "1 again", "a/1.mp4", now=NOW)
    library.add_episode(conn, a, "gone", "a/missing.mp4", now=NOW)
    library.add_episode(conn, b, "2", "b/2.mp4", now=NOW)
    _source(conn, "v2", file_path="src/orphan.mp4")  # no show: total only
    usage = library.disk_usage(conn, media)
    assert usage == library.DiskUsage(total_bytes=1322, per_show={a: 1015, b: 300, empty: 0})


def test_delete_episode_removes_unshared_files(conn, tmp_path):
    media = tmp_path / "media"
    sid = library.create_show(conn, "S", now=NOW)
    f1, t1 = _file(media, "s/1.mp4", 10), _file(media, "s/1.jpg", 1)
    shared = _file(media, "s/shared.mp4", 10)
    e1 = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW, thumbnail_path="s/1.jpg")
    e2 = library.add_episode(conn, sid, "2", "s/shared.mp4", now=NOW)
    library.add_episode(conn, sid, "3", "s/shared.mp4", now=NOW)
    library.delete_episode(conn, media, e1)
    library.delete_episode(conn, media, e2)
    assert library.get_episode(conn, e1) is None and library.get_episode(conn, e2) is None
    assert not f1.exists() and not t1.exists()
    assert shared.exists()
    with pytest.raises(KeyError):
        library.delete_episode(conn, media, e1)


def test_delete_episode_drops_its_now_unused_source(conn, tmp_path):
    # v1: episode and source video share one MP4; deleting the episode must free the disk (CI-6).
    media = tmp_path / "media"
    sid = library.create_show(conn, "S", now=NOW)
    mp4, jpg = _file(media, "s/v.mp4", 10), _file(media, "s/v.jpg", 1)
    src = _source(conn, "vid", show_id=sid, file_path="s/v.mp4", thumbnail_path="s/v.jpg")
    e1 = library.add_episode(conn, sid, "part 1", "s/v.mp4", now=NOW, source_video_id=src)
    e2 = library.add_episode(conn, sid, "part 2", "s/v.mp4", now=NOW, source_video_id=src)
    library.delete_episode(conn, media, e1)
    assert mp4.exists() and conn.execute("SELECT 1 FROM source_video WHERE id = ?", (src,)).fetchone()
    library.delete_episode(conn, media, e2)
    assert not mp4.exists() and not jpg.exists()
    assert conn.execute("SELECT 1 FROM source_video WHERE id = ?", (src,)).fetchone() is None


def test_delete_episode_ignores_missing_file(conn, tmp_path):
    sid = library.create_show(conn, "S", now=NOW)
    eid = library.add_episode(conn, sid, "1", "s/missing.mp4", now=NOW)
    library.delete_episode(conn, tmp_path, eid)
    assert library.get_episode(conn, eid) is None


def test_delete_never_touches_files_outside_media_dir(conn, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    outside = _file(tmp_path, "precious.mp4", 10)
    (media / "link.mp4").symlink_to(outside)
    sid = library.create_show(conn, "S", now=NOW)
    e1 = library.add_episode(conn, sid, "1", "../precious.mp4", now=NOW)
    e2 = library.add_episode(conn, sid, "2", str(outside), now=NOW)
    e3 = library.add_episode(conn, sid, "3", "link.mp4", now=NOW)
    for eid in (e1, e2, e3):
        library.delete_episode(conn, media, eid)
    assert outside.exists()
    assert library.disk_usage(conn, media).total_bytes == 0


def test_delete_show_removes_episodes_sources_and_files(conn, tmp_path):
    media = tmp_path / "media"
    sid = library.create_show(conn, "S", now=NOW)
    keep = library.create_show(conn, "Keep", now=NOW)
    art = _file(media, "s/art.jpg", 1)
    ep = _file(media, "s/1.mp4", 10)
    src_file = _file(media, "s/src.mp4", 10)
    shared = _file(media, "shared.mp4", 10)
    conn.execute("UPDATE show SET artwork_path = 's/art.jpg' WHERE id = ?", (sid,))
    src = _source(conn, "v", show_id=sid, file_path="s/src.mp4")
    library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW, source_video_id=src)
    library.add_episode(conn, sid, "2", "shared.mp4", now=NOW)
    library.add_episode(conn, keep, "k", "shared.mp4", now=NOW)
    library.delete_show(conn, media, sid)
    assert library.get_show(conn, sid) is None
    assert conn.execute("SELECT COUNT(*) FROM episode WHERE show_id = ?", (sid,)).fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM source_video WHERE id = ?", (src,)).fetchone() is None
    assert not art.exists() and not ep.exists() and not src_file.exists()
    assert shared.exists()
    assert not (media / "s").exists()  # emptied directory removed
    with pytest.raises(KeyError):
        library.delete_show(conn, media, sid)


def test_delete_show_keeps_source_used_by_another_show(conn, tmp_path):
    # LM-1: an episode moved to another show keeps its source video alive.
    media = tmp_path / "media"
    sid = library.create_show(conn, "S", now=NOW)
    other = library.create_show(conn, "Other", now=NOW)
    mp4 = _file(media, "s/v.mp4", 10)
    src = _source(conn, "v", show_id=sid, file_path="s/v.mp4")
    moved = library.add_episode(conn, other, "moved", "s/v.mp4", now=NOW, source_video_id=src)
    library.delete_show(conn, media, sid)
    assert mp4.exists()
    assert library.get_episode(conn, moved).source_video_id == src


# --- Step 5 (admin pages): library management (CI-4, CI-6, LM-1..LM-4) ---


def test_list_shows(conn):
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    assert [s.id for s in library.list_shows(conn)] == [a, b]


def test_set_show_autoplay(conn):
    sid = library.create_show(conn, "S", now=NOW)
    library.set_show_autoplay(conn, sid, False)
    assert library.get_show(conn, sid).autoplay is False
    library.set_show_autoplay(conn, sid, True)
    assert library.get_show(conn, sid).autoplay is True
    with pytest.raises(KeyError):
        library.set_show_autoplay(conn, 999, True)


def test_rename_episode(conn):
    sid = library.create_show(conn, "S", now=NOW)
    eid = library.add_episode(conn, sid, "Old", "s/e.mp4", now=NOW)
    library.rename_episode(conn, eid, "  New  ")
    assert library.get_episode(conn, eid).title == "New"
    with pytest.raises(ValueError):
        library.rename_episode(conn, eid, "   ")
    with pytest.raises(KeyError):
        library.rename_episode(conn, 999, "x")


def test_move_episode_appends_at_end(conn):
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    library.add_episode(conn, b, "b1", "b/1.mp4", now=NOW)
    e = library.add_episode(conn, a, "a1", "a/1.mp4", now=NOW)
    library.move_episode(conn, e, b)
    ep = library.get_episode(conn, e)
    assert ep.show_id == b
    assert ep.sort_order == 1  # after b1
    assert [x.id for x in library.list_episodes(conn, b, include_hidden=True)][-1] == e


def test_move_episode_unknown_ids(conn):
    a = library.create_show(conn, "A", now=NOW)
    e = library.add_episode(conn, a, "a1", "a/1.mp4", now=NOW)
    with pytest.raises(KeyError):
        library.move_episode(conn, 999, a)
    with pytest.raises(KeyError):
        library.move_episode(conn, e, 999)


def test_move_episode_source_follows_when_no_episodes_left(conn):
    # LM-1: moving the only episode using a source video takes the source with it.
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    src = _source(conn, "v", show_id=a)
    e = library.add_episode(conn, a, "a1", "a/1.mp4", now=NOW, source_video_id=src)
    library.move_episode(conn, e, b)
    assert conn.execute("SELECT show_id FROM source_video WHERE id = ?", (src,)).fetchone()[0] == b


def test_move_episode_source_stays_when_sibling_remains(conn):
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    src = _source(conn, "v", show_id=a)
    e1 = library.add_episode(conn, a, "a1", "a/1.mp4", now=NOW, source_video_id=src)
    library.add_episode(conn, a, "a2", "a/1.mp4", now=NOW, source_video_id=src)
    library.move_episode(conn, e1, b)
    assert conn.execute("SELECT show_id FROM source_video WHERE id = ?", (src,)).fetchone()[0] == a


def test_move_episode_step_swaps_neighbours(conn):
    sid = library.create_show(conn, "S", now=NOW)
    e1 = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW)
    e2 = library.add_episode(conn, sid, "2", "s/2.mp4", now=NOW)
    e3 = library.add_episode(conn, sid, "3", "s/3.mp4", now=NOW)
    library.move_episode_step(conn, e2, "up")
    assert [e.id for e in library.list_episodes(conn, sid)] == [e2, e1, e3]
    library.move_episode_step(conn, e2, "down")
    assert [e.id for e in library.list_episodes(conn, sid)] == [e1, e2, e3]


def test_move_episode_step_noop_at_ends(conn):
    sid = library.create_show(conn, "S", now=NOW)
    e1 = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW)
    e2 = library.add_episode(conn, sid, "2", "s/2.mp4", now=NOW)
    library.move_episode_step(conn, e1, "up")
    assert [e.id for e in library.list_episodes(conn, sid)] == [e1, e2]
    library.move_episode_step(conn, e2, "down")
    assert [e.id for e in library.list_episodes(conn, sid)] == [e1, e2]


def test_move_episode_step_normalises_ties(conn):
    sid = library.create_show(conn, "S", now=NOW)
    e1 = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW, sort_order=5)
    e2 = library.add_episode(conn, sid, "2", "s/2.mp4", now=NOW, sort_order=5)  # tie: by id after e1
    e3 = library.add_episode(conn, sid, "3", "s/3.mp4", now=NOW, sort_order=5)  # tie: by id after e2
    library.move_episode_step(conn, e3, "up")
    assert [e.id for e in library.list_episodes(conn, sid)] == [e1, e3, e2]
    assert [e.sort_order for e in library.list_episodes(conn, sid)] == [0, 1, 2]


def test_move_episode_step_errors(conn):
    sid = library.create_show(conn, "S", now=NOW)
    e1 = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW)
    with pytest.raises(KeyError):
        library.move_episode_step(conn, 999, "up")
    with pytest.raises(ValueError):
        library.move_episode_step(conn, e1, "sideways")


def test_set_show_artwork_replaces_and_removes_old(conn, tmp_path):
    media = tmp_path / "media"
    sid = library.create_show(conn, "S", now=NOW)
    old = _file(media, "art/old.jpg", 5)
    new = _file(media, "art/new.jpg", 7)
    library.set_show_artwork(conn, media, sid, "art/old.jpg")
    assert library.get_show(conn, sid).artwork_path == "art/old.jpg"
    library.set_show_artwork(conn, media, sid, "art/new.jpg")
    assert library.get_show(conn, sid).artwork_path == "art/new.jpg"
    assert not old.exists()
    assert new.exists()
    with pytest.raises(KeyError):
        library.set_show_artwork(conn, media, 999, "art/new.jpg")


def test_set_show_artwork_keeps_file_still_referenced(conn, tmp_path):
    media = tmp_path / "media"
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    shared = _file(media, "art/shared.jpg", 3)
    library.set_show_artwork(conn, media, a, "art/shared.jpg")
    library.set_show_artwork(conn, media, b, "art/shared.jpg")
    library.set_show_artwork(conn, media, a, "art/other.jpg")
    assert shared.exists()  # still referenced by b


def test_set_episode_thumbnail_replaces_and_removes_old(conn, tmp_path):
    media = tmp_path / "media"
    sid = library.create_show(conn, "S", now=NOW)
    eid = library.add_episode(conn, sid, "1", "s/1.mp4", now=NOW)
    old = _file(media, "thumbs/old.jpg", 5)
    new = _file(media, "thumbs/new.jpg", 7)
    library.set_episode_thumbnail(conn, media, eid, "thumbs/old.jpg")
    library.set_episode_thumbnail(conn, media, eid, "thumbs/new.jpg")
    assert library.get_episode(conn, eid).thumbnail_path == "thumbs/new.jpg"
    assert not old.exists()
    assert new.exists()
    with pytest.raises(KeyError):
        library.set_episode_thumbnail(conn, media, 999, "thumbs/new.jpg")


def test_list_shows_for_admin(conn, tmp_path):
    media = tmp_path / "media"
    a = library.create_show(conn, "A", now=NOW)
    b = library.create_show(conn, "B", now=NOW)
    e1 = library.add_episode(conn, a, "1", "a/1.mp4", now=NOW)
    library.add_episode(conn, a, "2", "a/2.mp4", now=NOW)
    library.set_episode_hidden(conn, e1, True)
    _file(media, "a/1.mp4", 10)
    _file(media, "a/2.mp4", 20)
    items, total = library.list_shows_for_admin(conn, media)
    by_id = {i.show.id: i for i in items}
    assert by_id[a].episode_count == 2
    assert by_id[a].hidden_episode_count == 1
    assert by_id[a].disk_bytes == 30
    assert by_id[b].episode_count == 0
    assert by_id[b].disk_bytes == 0
    assert total == 30
    assert [i.show.id for i in items] == [a, b]  # sort_order, id


def test_list_held_downloads_and_publish(conn):
    ready_show = library.create_show(conn, "S", now=NOW)
    ready_src = _source(conn, "ready1")
    conn.execute("UPDATE source_video SET publish = 'hold', status = 'ready' WHERE id = ?", (ready_src,))
    ready_ep = library.add_episode(
        conn, ready_show, "Held ep", "s/1.mp4", now=NOW, source_video_id=ready_src, hidden=True
    )
    queued_src = _source(conn, "queued1")
    conn.execute("UPDATE source_video SET publish = 'hold', status = 'queued' WHERE id = ?", (queued_src,))
    published_src = _source(conn, "pub1")  # publish='publish' by default: not held

    held = library.list_held_downloads(conn)
    assert {h.source_id for h in held} == {ready_src, queued_src}
    by_id = {h.source_id: h for h in held}
    assert by_id[ready_src].episode_id == ready_ep
    assert by_id[ready_src].status == "ready"
    assert by_id[queued_src].episode_id is None
    assert by_id[queued_src].status == "queued"
    assert published_src not in by_id

    library.publish_held(conn, ready_src, now=NOW)
    assert library.get_episode(conn, ready_ep).hidden is False
    assert conn.execute("SELECT publish FROM source_video WHERE id = ?", (ready_src,)).fetchone()[0] == "publish"
    assert ready_src not in {h.source_id for h in library.list_held_downloads(conn)}

    with pytest.raises(KeyError):
        library.publish_held(conn, 999, now=NOW)
    with pytest.raises(KeyError):
        library.publish_held(conn, ready_src, now=NOW)  # already published, no longer held


def _held(conn, youtube_id, *, status="ready", playlist=("PL1", "Songs"), show_id=None):
    """A held source from a playlist, with a hidden episode once it is ready."""
    src = _source(conn, youtube_id)
    conn.execute(
        "UPDATE source_video SET publish = 'hold', status = ?, playlist_id = ?, playlist_title = ? WHERE id = ?",
        (status, *(playlist or (None, None)), src),
    )
    ep = None
    if status == "ready":
        show_id = show_id or library.create_show(conn, youtube_id, now=NOW)
        ep = library.add_episode(conn, show_id, youtube_id, f"s/{youtube_id}.mp4", now=NOW,
                                 source_video_id=src, hidden=True)
    return src, ep


def test_list_held_downloads_includes_playlist(conn):  # CI-7
    from_pl, _ = _held(conn, "a", status="queued")
    single, _ = _held(conn, "b", status="queued", playlist=None)
    by_id = {h.source_id: h for h in library.list_held_downloads(conn)}
    assert (by_id[from_pl].playlist_id, by_id[from_pl].playlist_title) == ("PL1", "Songs")
    assert (by_id[single].playlist_id, by_id[single].playlist_title) == (None, None)


def test_publish_held_playlist_publishes_only_ready_videos_of_that_playlist(conn):  # CI-7
    show = library.create_show(conn, "S", now=NOW)
    ready1, ep1 = _held(conn, "r1", show_id=show)
    ready2, ep2 = _held(conn, "r2", show_id=show)
    downloading, _ = _held(conn, "d1", status="downloading")
    failed, _ = _held(conn, "f1", status="failed")
    other, other_ep = _held(conn, "o1", playlist=("PL2", "Other"))

    assert library.publish_held_playlist(conn, "PL1", now=NOW) == 2
    assert not library.get_episode(conn, ep1).hidden and not library.get_episode(conn, ep2).hidden
    assert library.get_episode(conn, other_ep).hidden
    held = {h.source_id for h in library.list_held_downloads(conn)}
    assert held == {downloading, failed, other}

    assert library.publish_held_playlist(conn, "PL1", now=NOW) == 0  # still held, none ready yet
    conn.execute("UPDATE source_video SET status = 'ready' WHERE id = ?", (downloading,))
    assert library.publish_held_playlist(conn, "PL1", now=NOW) == 1


def test_publish_held_playlist_unknown(conn):
    _held(conn, "a")
    library.publish_held_playlist(conn, "PL1", now=NOW)
    with pytest.raises(KeyError):
        library.publish_held_playlist(conn, "PL1", now=NOW)  # nothing held any more
    with pytest.raises(KeyError):
        library.publish_held_playlist(conn, "nope", now=NOW)


def test_list_held_downloads_has_one_row_per_source(conn):  # a split source has several episodes
    show = library.create_show(conn, "S", now=NOW)
    src = _source(conn, "split1")
    conn.execute("UPDATE source_video SET publish = 'hold', status = 'ready' WHERE id = ?", (src,))
    ids = [library.add_episode(conn, show, f"Part {i}", f"s/{i}.mp4", now=NOW, source_video_id=src, hidden=True)
           for i in range(3)]
    (held,) = library.list_held_downloads(conn)
    assert held.source_id == src and held.episode_id == ids[0]
