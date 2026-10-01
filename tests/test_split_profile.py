"""Smart splitting (v6) contract: hashing, the synthetic compilations, and profile storage."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest

from tellybox import db, detect, jobs, library, splitting
from tests.detect_clips import LOGO_REGION, make_compilation

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture(scope="session")
def compilation(tmp_path_factory):
    return make_compilation(tmp_path_factory.mktemp("detect") / "comp.mp4", [12, 15, 10], weak={1})


# --- detect: shared types and hashing ------------------------------------------------------------


def test_region_bounds_and_json():
    r = detect.Region(0.1, 0.2, 0.3, 0.4)
    assert detect.Region.from_json(r.to_json()) == r
    assert detect.Region.from_json(None) is None
    for bad in ((-0.1, 0, 0.5, 0.5), (0.8, 0, 0.5, 0.5), (0, 0, 0, 0.5), (0, 0.9, 0.5, 0.2)):
        with pytest.raises(ValueError):
            detect.Region(*bad)


def test_region_crop():
    import numpy as np

    frame = np.zeros((180, 320), dtype=np.uint8)
    assert detect.Region(0.5, 0.5, 0.5, 0.5).crop(frame).shape == (90, 160)
    assert detect.Region(0.999, 0, 0.001, 1).crop(frame).shape == (180, 1)  # never empty


@needs_ffmpeg
def test_reference_hash_is_stable_and_region_specific(compilation):
    region = detect.Region(*LOGO_REGION)
    a = detect.reference_hash(compilation.reference, region)
    assert a == detect.reference_hash(compilation.reference, region)
    assert a != detect.reference_hash(compilation.reference, None)
    assert len(a) == 16  # 64-bit
    with pytest.raises(ValueError):
        detect.reference_hash(b"not an image", None)


@needs_ffmpeg
def test_compilation_layout(compilation):
    assert len(compilation.card_starts) == 3 and compilation.titles == ["Episode 1", "Episode 2", "Episode 3"]
    assert compilation.black_starts == [3.0, 3.0 + 0.6 + 2 + 12, 3.0 + 2 * 2.6 + 12 + 15]
    assert all(c - b == pytest.approx(0.6) for b, c in zip(compilation.black_starts, compilation.card_starts))
    from tellybox import media_format

    assert media_format.probe(compilation.path).duration_s == pytest.approx(compilation.duration_s, abs=0.1)


# --- library: profiles, references, detect requests, publish whole -------------------------------


@pytest.fixture
def conn():
    c = db.open_db(":memory:")
    yield c
    c.close()


def _source(conn, *, awaiting=False, publish="publish", file_path="shows/1/abc.mp4", status="ready"):
    show_id = library.create_show(conn, "Bluey", now=NOW)
    ts = db.to_db(NOW)
    sid = conn.execute(
        """INSERT INTO source_video (youtube_id, url, title, duration_s, file_path, show_id, publish, status,
                                     awaiting_split, created_at, updated_at)
           VALUES ('abc', 'https://youtu.be/abc', 'Compilation', 900, ?, ?, ?, ?, ?, ?, ?)""",
        (file_path, show_id, publish, status, int(awaiting), ts, ts),
    ).lastrowid
    library.add_episode(conn, show_id, "Compilation", "shows/1/abc.mp4", now=NOW, duration_s=900,
                        source_video_id=sid, hidden=awaiting or publish == "hold")
    return show_id, sid


def test_profile_defaults_and_save(conn):
    show_id, _ = _source(conn)
    p = library.get_split_profile(conn, show_id)
    assert (p.stored, p.match_threshold, p.snap_window_s, p.usable) == (False, detect.DEFAULT_THRESHOLD, 30.0, False)
    library.save_split_profile(conn, show_id, match_threshold=8, length_hint_s=420, snap_window_s=20, ocr=True,
                               ocr_region=[0.1, 0.5, 0.8, 0.3], auto_detect=True, now=NOW)
    p = library.get_split_profile(conn, show_id)
    assert (p.stored, p.match_threshold, p.length_hint_s, p.ocr, p.ocr_region, p.auto_detect) == (
        True, 8, 420, True, [0.1, 0.5, 0.8, 0.3], True)


@pytest.mark.parametrize("kwargs", [
    {"match_threshold": 40}, {"length_hint_s": 5}, {"snap_window_s": 500}, {"ocr_region": [0, 0, 2, 1]},
])
def test_profile_refuses_out_of_range(conn, kwargs):
    show_id, _ = _source(conn)
    values = dict(match_threshold=6, length_hint_s=None, snap_window_s=30, ocr=False, ocr_region=None,
                  auto_detect=False) | kwargs
    with pytest.raises(ValueError):
        library.save_split_profile(conn, show_id, now=NOW, **values)


@needs_ffmpeg
def test_references_add_delete_and_go_with_the_show(conn, tmp_path, compilation):
    show_id, sid = _source(conn)
    ref_id = library.add_split_reference(conn, tmp_path, show_id, compilation.reference, LOGO_REGION,
                                         source_video_id=sid, at_s=4.6, now=NOW)
    [ref] = library.get_split_profile(conn, show_id).references
    assert (ref.id, ref.region, ref.source_video_id, ref.at_s) == (ref_id, LOGO_REGION, sid, 4.6)
    assert ref.card_hash == detect.reference_hash(compilation.reference, detect.Region(*LOGO_REGION))
    assert (tmp_path / ref.image_path).is_file() and ref.image_path.startswith("split/")
    library.delete_split_reference(conn, tmp_path, ref_id)
    assert not (tmp_path / ref.image_path).exists()
    with pytest.raises(KeyError):
        library.delete_split_reference(conn, tmp_path, ref_id)
    ref_id = library.add_split_reference(conn, tmp_path, show_id, compilation.reference, None,
                                         source_video_id=None, at_s=None, now=NOW)
    path = tmp_path / library.get_split_profile(conn, show_id).references[0].image_path
    library.delete_show(conn, tmp_path, show_id)
    assert not path.exists()


def test_add_reference_refuses_bad_region(conn, tmp_path):
    show_id, _ = _source(conn)
    with pytest.raises(ValueError):
        library.add_split_reference(conn, tmp_path, show_id, b"x", [0, 0, 1], source_video_id=None, at_s=None, now=NOW)


def _with_reference(conn, show_id):
    conn.execute(
        "INSERT INTO split_reference (show_id, image_path, card_hash, created_at) VALUES (?, 'split/x.jpg', ?, ?)",
        (show_id, "0" * 16, db.to_db(NOW)),
    )


def test_request_detect(conn):
    show_id, sid = _source(conn)
    with pytest.raises(LookupError):  # no title card marked yet
        library.request_detect(conn, sid, now=NOW)
    _with_reference(conn, show_id)
    job_id = library.request_detect(conn, sid, now=NOW)
    job = jobs.get(conn, job_id)
    assert (job.type, job.target_id, job.max_attempts) == (jobs.JobType.DETECT, sid, 2)
    with pytest.raises(library.SplitLocked):
        library.request_detect(conn, sid, now=NOW)
    with pytest.raises(KeyError):
        library.request_detect(conn, 999, now=NOW)


def test_request_detect_refuses_without_file(conn):
    show_id, sid = _source(conn, file_path=None)
    _with_reference(conn, show_id)
    with pytest.raises(library.SourceGone):
        library.request_detect(conn, sid, now=NOW)


def test_save_detected_split_and_review_list(conn):
    show_id, sid = _source(conn, awaiting=True)
    segs = splitting.segments_from_cuts(900, [300, 600], ["A", "B", "C"])
    meta = [{"at_s": 300, "title_hit_s": 302.5, "confidence": 0.9, "snapped": "black", "title": "B"},
            {"at_s": 600, "title_hit_s": 600, "confidence": 0.5, "snapped": None, "title": "C"}]
    library.save_detected_split(conn, sid, segs, meta, now=NOW)
    p = library.get_split(conn, sid)
    assert (p.status, p.origin, p.segments) == ("review", "detected", segs)
    assert library.get_detected(conn, sid) == meta
    [item] = library.list_splits_for_review(conn)
    assert (item.status, item.origin, item.awaiting_split, item.parts) == ("review", "detected", True, 3)
    library.save_split(conn, sid, splitting.segments_from_cuts(900, [300]), now=NOW)  # the admin edits
    assert library.get_split(conn, sid).origin == "detected" and library.get_detected(conn, sid) == meta
    library.approve_split(conn, sid, delete_source=False, now=NOW)
    with pytest.raises(library.SplitLocked):
        library.save_detected_split(conn, sid, segs, meta, now=NOW)


def test_get_detected_none_for_manual(conn):
    _show_id, sid = _source(conn)
    library.save_split(conn, sid, splitting.segments_from_cuts(900, [300]), now=NOW, origin="manual")
    assert library.get_detected(conn, sid) is None


def test_publish_unsplit(conn):
    _show_id, sid = _source(conn, awaiting=True)
    library.publish_unsplit(conn, sid, now=NOW)
    assert not library.list_source_episodes(conn, sid)[0].hidden
    assert conn.execute("SELECT awaiting_split FROM source_video WHERE id = ?", (sid,)).fetchone()[0] == 0
    with pytest.raises(KeyError):
        library.publish_unsplit(conn, sid, now=NOW)


def test_publish_unsplit_keeps_a_held_download_hidden(conn):
    _show_id, sid = _source(conn, awaiting=True, publish="hold")
    library.publish_unsplit(conn, sid, now=NOW)
    assert library.list_source_episodes(conn, sid)[0].hidden


def test_migration_010(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    for version, _name, sql in db.migrations():
        if version <= 9:
            for statement in db._split_sql(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
    ts = db.to_db(NOW)
    conn.execute("INSERT INTO source_video (youtube_id, url, title, status, created_at, updated_at) "
                 "VALUES ('a', 'u', 't', 'ready', ?, ?)", (ts, ts))
    assert db.migrate(conn) >= 10
    assert conn.execute("SELECT awaiting_split FROM source_video").fetchone()[0] == 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(split_proposal)")}
    assert "detected_json" in cols
