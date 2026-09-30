"""SponsorBlock categories, segments and position remapping (SB-1..SB-3)."""

import pytest

from tellybox import db, sponsorblock
from tellybox.sponsorblock import Segment, from_original, remap_position, same_segments, to_original


def seg(start, end, cat="sponsor"):
    return Segment(cat, float(start), float(end))


def chapter(start, end, cat="sponsor", type="skip"):
    return {"start_time": start, "end_time": end, "category": cat, "title": cat, "type": type}


# --- categories ---------------------------------------------------------------


def test_csv_roundtrip_keeps_known_in_order():
    assert sponsorblock.parse_csv("interaction, sponsor,bogus") == ["sponsor", "interaction"]
    assert sponsorblock.parse_csv(None) == []
    assert sponsorblock.parse_csv("") == []
    assert sponsorblock.to_csv(["interaction", "sponsor", "nope"]) == "sponsor,interaction"
    assert sponsorblock.to_csv([]) == ""


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.db")
    yield c
    c.close()


def _show(conn, categories):
    cur = conn.execute(
        "INSERT INTO show (name, created_at, sponsorblock_categories) VALUES ('s', '2026-01-01T00:00:00Z', ?)",
        (categories,),
    )
    return cur.lastrowid


def test_effective_categories(conn):
    assert sponsorblock.effective_categories(conn, None) == list(sponsorblock.DEFAULT)
    assert sponsorblock.effective_categories(conn, _show(conn, None)) == list(sponsorblock.DEFAULT)
    assert sponsorblock.effective_categories(conn, _show(conn, "")) == []
    assert sponsorblock.effective_categories(conn, _show(conn, "intro,sponsor")) == ["sponsor", "intro"]
    conn.execute("UPDATE settings SET sponsorblock_categories = ''")
    assert sponsorblock.effective_categories(conn, None) == []
    assert sponsorblock.effective_categories(conn, 9999) == []  # unknown show → global


# --- segments -----------------------------------------------------------------


def test_segments_from_info_filters_sorts_merges():
    chapters = [
        chapter(100, 110, "selfpromo"),
        chapter(10, 20),
        chapter(15, 25, "interaction"),  # overlaps → merged into the sponsor one
        chapter(50, 51, "poi_highlight", type="poi"),
        chapter(60, 70, "intro"),  # not enabled
        chapter(80, 80),  # empty
    ]
    got = sponsorblock.segments_from_info(chapters, ["sponsor", "selfpromo", "interaction"])
    assert got == [seg(10, 25), seg(100, 110, "selfpromo")]
    assert sponsorblock.removed_s(got) == 25
    assert sponsorblock.segments_from_info(None, ["sponsor"]) == []


def test_same_segments_tolerance():
    a = [seg(10, 20), seg(30, 40)]
    assert same_segments(a, [seg(10.2, 19.9, "selfpromo"), seg(30, 40)])
    assert not same_segments(a, [seg(10, 20)])
    assert not same_segments(a, [seg(10, 21), seg(30, 40)])
    assert same_segments([], [])


def test_dumps_loads():
    a = [seg(1, 2), seg(3.5, 4, "interaction")]
    assert sponsorblock.loads(sponsorblock.dumps(a)) == a
    assert sponsorblock.loads(None) == []


# --- remapping ----------------------------------------------------------------


def test_to_and_from_original():
    cuts = [seg(10, 20), seg(30, 40)]  # file: 0-10 | 20-30 → 10-20 | 40- → 20-
    assert to_original(5, cuts) == 5
    assert to_original(10, cuts) == 20
    assert to_original(15, cuts) == 25
    assert to_original(25, cuts) == 45
    assert from_original(5, cuts) == 5
    assert from_original(25, cuts) == 15
    assert from_original(45, cuts) == 25
    assert from_original(15, cuts) == 10  # inside a cut → where it was
    assert from_original(35, cuts) == 20


@pytest.mark.parametrize(
    "pos, old, new, expected",
    [
        (50, [], [], 50),
        (50, [], [seg(10, 20)], 40),  # a new cut before the position moves it back (SB-3)
        (15, [], [seg(10, 20)], 10),  # inside the new cut → its start
        (5, [], [seg(10, 20)], 5),  # cut after the position: unchanged
        (40, [seg(10, 20)], [], 50),  # cut undone ("download again without SponsorBlock")
        (40, [seg(10, 20)], [seg(10, 20), seg(60, 70)], 40),  # only a later cut added
        (40, [seg(10, 20)], [seg(10, 20), seg(30, 35)], 35),
        (0, [seg(0, 5)], [seg(0, 8)], 0),
    ],
)
def test_remap_position(pos, old, new, expected):
    assert remap_position(pos, old, new) == pytest.approx(expected)
