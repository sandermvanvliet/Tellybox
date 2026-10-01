"""Episode splitting (v5): the plan module, its storage in the library, and frame-accurate cutting."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tellybox import db, jobs, library, media_format, sponsorblock, splitting
from tellybox.splitting import Segment, SplitInvalid

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
has_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)


# --- pure plan -----------------------------------------------------------------------------------


def test_segments_from_cuts_sorts_and_ignores_out_of_range():
    segs = splitting.segments_from_cuts(100, [60, 30, 30, 0, 100, 150], ["a", "b"])
    assert segs == [Segment(0, 30, "a"), Segment(30, 60, "b"), Segment(60, 100, "")]
    assert splitting.cuts_of(segs) == [30, 60]


def test_whole():
    assert splitting.whole(12.3456, "T") == [Segment(0, 12.346, "T")]


def test_segments_from_chapters_names_parts():
    chapters = [
        {"start_s": 0, "end_s": 300, "title": " Intro "},
        {"start_s": 300, "end_s": 600, "title": "Keepy Uppy"},
        {"start_s": 600, "end_s": 900, "title": "Camping"},
    ]
    assert splitting.segments_from_chapters(chapters, 900) == [
        Segment(0, 300, "Intro"), Segment(300, 600, "Keepy Uppy"), Segment(600, 900, "Camping"),
    ]


def test_segments_from_chapters_first_chapter_late():
    segs = splitting.segments_from_chapters([{"start_s": 10, "end_s": 50, "title": "One"}], 50)
    assert segs == [Segment(0, 10, ""), Segment(10, 50, "One")]


def test_chapters_on_file_maps_original_timeline_when_cut():
    cuts = [sponsorblock.Segment("sponsor", 100, 130)]
    chapters = [{"start_s": 0, "end_s": 200, "title": "A"}, {"start_s": 200, "end_s": 400, "title": "B"}]
    assert splitting.chapters_on_file(chapters, 370, cuts) == [
        {"start_s": 0, "end_s": 170, "title": "A"}, {"start_s": 170, "end_s": 370, "title": "B"},
    ]


def test_chapters_on_file_leaves_shifted_chapters_alone():
    cuts = [sponsorblock.Segment("sponsor", 100, 130)]
    chapters = [{"start_s": 0, "end_s": 170, "title": "A"}, {"start_s": 170, "end_s": 370, "title": "B"}]
    assert splitting.chapters_on_file(chapters, 370, cuts) == chapters
    assert splitting.chapters_on_file(None, 370, cuts) == []


@pytest.mark.parametrize(
    "segments, message",
    [
        ([], "no parts"),
        ([Segment(1, 100)], "beginning"),
        ([Segment(0, 50), Segment(50, 90)], "end of the video"),
        ([Segment(0, 50), Segment(51, 100)], "gaps"),
        ([Segment(0, 3), Segment(3, 100)], "at least 5 seconds"),
        ([Segment(0, 50, keep=False), Segment(50, 100, keep=False)], "Keep at least one"),
        ([Segment(0, 100)], "Add a cut"),
        ([Segment(0, 50, "x" * 201), Segment(50, 100)], "at most 200"),
    ],
)
def test_validate_refuses(segments, message):
    with pytest.raises(SplitInvalid, match=message):
        splitting.validate(segments, 100)


def test_validate_accepts_trimmed_and_short_dropped_parts():
    splitting.validate([Segment(0, 2, keep=False), Segment(2, 100)], 100)
    splitting.validate([Segment(0, 50), Segment(50, 99.5)], 100)  # end within tolerance


def test_check_shape_allows_unfinished_drafts():
    splitting.check_shape([Segment(0, 100)], 100)
    splitting.check_shape([Segment(0, 2), Segment(2, 100)], 100)


def test_kept_titles_and_file_names():
    segs = [Segment(0, 5, "intro", keep=False), Segment(5, 50, ""), Segment(50, 100, "Camping")]
    assert splitting.kept_titles(segs, "Bluey compilation") == ["Bluey compilation (1)", "Camping"]
    assert splitting.episode_file_name("abc", 12, 3) == "abc-s12-03.mp4"


def test_json_round_trip_and_malformed():
    segs = [Segment(0, 5.123, "a", keep=False), Segment(5.123, 9)]
    assert splitting.loads(splitting.dumps(segs)) == segs
    assert splitting.from_dict({"start_s": "1.5", "end_s": 3}) == Segment(1.5, 3, "", True)
    with pytest.raises(SplitInvalid):
        splitting.from_dict({"start_s": 1})
    with pytest.raises(SplitInvalid):
        splitting.from_dict({"start_s": "x", "end_s": 3})


# --- storage -------------------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = db.open_db(":memory:")
    yield c
    c.close()


def _source(conn, *, chapters=None, duration=900.0, status="ready", file_path="shows/1/abc.mp4", sb=None):
    show_id = library.create_show(conn, "Bluey", now=NOW)
    ts = db.to_db(NOW)
    sid = conn.execute(
        """INSERT INTO source_video (youtube_id, url, title, duration_s, chapters_json, file_path, show_id,
                                     publish, status, sb_segments_json, created_at, updated_at)
           VALUES ('abc', 'https://youtu.be/abc', 'Compilation', ?, ?, ?, ?, 'publish', ?, ?, ?, ?)""",
        (duration, json.dumps(chapters) if chapters else None, file_path, show_id, status,
         sponsorblock.dumps(sb) if sb else None, ts, ts),
    ).lastrowid
    library.add_episode(conn, show_id, "Compilation", "shows/1/abc.mp4", now=NOW, duration_s=duration,
                        source_video_id=sid)
    return sid


CHAPTERS = [
    {"start_s": 0, "end_s": 300, "title": "One"},
    {"start_s": 300, "end_s": 600, "title": "Two"},
    {"start_s": 600, "end_s": 900, "title": "Three"},
]


def test_get_split_unsaved_from_chapters(conn):
    sid = _source(conn, chapters=CHAPTERS)
    p = library.get_split(conn, sid)
    assert (p.stored, p.status, p.origin, p.editable) == (False, "draft", "chapters", True)
    assert [s.title for s in p.segments] == ["One", "Two", "Three"]
    assert p.chapters == CHAPTERS and p.duration_s == 900


def test_get_split_unsaved_whole_without_chapters(conn):
    sid = _source(conn)
    p = library.get_split(conn, sid)
    assert (p.origin, p.segments) == ("manual", [Segment(0, 900, "Compilation")])


def test_get_split_none_until_ready(conn):
    assert library.get_split(conn, _source(conn, status="downloading")) is None
    assert library.get_split(conn, 999) is None


def test_save_split_upserts_and_keeps_origin(conn):
    sid = _source(conn, chapters=CHAPTERS)
    segs = splitting.segments_from_cuts(900, [300])
    p = library.save_split(conn, sid, segs, now=NOW, origin="chapters")
    assert (p.stored, p.status, p.origin, p.segments) == (True, "draft", "chapters", segs)
    p = library.save_split(conn, sid, splitting.segments_from_cuts(900, [200]), now=NOW + timedelta(minutes=1))
    assert p.origin == "chapters" and splitting.cuts_of(p.segments) == [200]
    assert p.updated_at == NOW + timedelta(minutes=1)


def test_save_split_refuses_bad_shape_and_locked(conn):
    sid = _source(conn)
    with pytest.raises(SplitInvalid):
        library.save_split(conn, sid, [Segment(0, 400)], now=NOW)
    library.save_split(conn, sid, splitting.segments_from_cuts(900, [450]), now=NOW)
    library.approve_split(conn, sid, delete_source=False, now=NOW)
    with pytest.raises(library.SplitLocked):
        library.save_split(conn, sid, splitting.segments_from_cuts(900, [400]), now=NOW)
    with pytest.raises(KeyError):
        library.save_split(conn, 999, [], now=NOW)


def test_approve_split_queues_job(conn):
    sid = _source(conn)
    with pytest.raises(KeyError):  # nothing saved yet
        library.approve_split(conn, sid, delete_source=False, now=NOW)
    library.save_split(conn, sid, [Segment(0, 900)], now=NOW)
    with pytest.raises(SplitInvalid):
        library.approve_split(conn, sid, delete_source=False, now=NOW)
    library.save_split(conn, sid, splitting.segments_from_cuts(900, [450]), now=NOW)
    job_id = library.approve_split(conn, sid, delete_source=True, now=NOW)
    job = jobs.get(conn, job_id)
    assert (job.type, job.target_id, job.max_attempts) == (jobs.JobType.SPLIT, sid, 2)
    p = library.get_split(conn, sid)
    assert (p.status, p.delete_source, p.editable) == ("approved", True, False)
    with pytest.raises(library.SplitLocked):
        library.approve_split(conn, sid, delete_source=False, now=NOW)


def test_source_gone_after_delete(conn):
    sid = _source(conn)
    library.save_split(conn, sid, splitting.segments_from_cuts(900, [450]), now=NOW)
    conn.execute("UPDATE source_video SET file_path = NULL WHERE id = ?", (sid,))
    assert library.get_split(conn, sid).editable is False
    with pytest.raises(library.SourceGone):
        library.save_split(conn, sid, splitting.segments_from_cuts(900, [400]), now=NOW)
    with pytest.raises(library.SourceGone):
        library.approve_split(conn, sid, delete_source=False, now=NOW)


def test_mark_discard_and_review_list(conn):
    sid = _source(conn)
    library.save_split(conn, sid, splitting.segments_from_cuts(900, [300, 600]), now=NOW)
    [item] = library.list_splits_for_review(conn)
    assert (item.source_video_id, item.status, item.parts, item.title) == (sid, "draft", 3, "Compilation")
    assert item.episode_id is not None
    library.approve_split(conn, sid, delete_source=False, now=NOW)
    with pytest.raises(library.SplitLocked):
        library.discard_split(conn, sid)
    library.mark_split(conn, sid, "failed", now=NOW, error="boom")
    assert library.get_split(conn, sid).error == "boom"
    library.mark_split(conn, sid, "done", now=NOW)
    assert library.list_splits_for_review(conn) == []
    with pytest.raises(library.SplitLocked):
        library.discard_split(conn, sid)
    with pytest.raises(ValueError):
        library.mark_split(conn, sid, "draft", now=NOW)
    library.save_split(conn, sid, splitting.segments_from_cuts(900, [300]), now=NOW)  # re-split: draft again
    library.discard_split(conn, sid)
    assert library.get_split(conn, sid).stored is False
    library.discard_split(conn, sid)  # nothing saved: fine


def test_is_split_and_source_episodes(conn):
    sid = _source(conn)
    assert not library.is_split(conn, sid)
    conn.execute("UPDATE episode SET start_s = 0, end_s = 900 WHERE source_video_id = ?", (sid,))
    assert library.is_split(conn, sid)
    assert [e.title for e in library.list_source_episodes(conn, sid)] == ["Compilation"]


def test_migration_009_keeps_jobs_and_allows_new_types(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    for version, _name, sql in db.migrations():
        if version <= 8:
            for statement in db._split_sql(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
    ts = db.to_db(NOW)
    conn.execute(
        "INSERT INTO job (type, target_id, status, run_after, created_at, updated_at) VALUES ('redownload', 3, 'queued', ?, ?, ?)",
        (ts, ts, ts),
    )
    assert db.migrate(conn) >= 9
    assert [tuple(r) for r in conn.execute("SELECT type, target_id, status FROM job")] == [("redownload", 3, "queued")]
    for job_type in ("split", "detect"):
        conn.execute("INSERT INTO job (type, run_after, created_at, updated_at) VALUES (?, ?, ?, ?)", (job_type, ts, ts, ts))
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'job_queue'").fetchone()


# --- cutting -------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def red_blue_clip(tmp_path_factory) -> Path:
    """2 s red then 2 s blue, 25 fps, a single keyframe at 0: a keyframe-only cut would start red."""
    out = tmp_path_factory.mktemp("split") / "redblue.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "color=red:s=320x240:r=25:d=2", "-f", "lavfi", "-i", "color=blue:s=320x240:r=25:d=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
         "-c:v", "libx264", "-g", "1000", "-keyint_min", "1000", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(out)],
        check=True,
    )
    return out


def _frame_rgb(path: Path, *, last: bool = False) -> tuple[int, int, int]:
    seek = ["-sseof", "-0.1"] if last else []
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *seek, "-i", str(path),
         "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    return raw[0], raw[1], raw[2]


@has_ffmpeg
def test_cut_is_frame_accurate(red_blue_clip, tmp_path):
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    progress: list[float] = []
    media_format.cut(red_blue_clip, first, 0, 2.0, on_progress=progress.append)
    media_format.cut(red_blue_clip, second, 2.0, 4.0)
    for part in (first, second):
        media_format.verify(part)
        assert media_format.probe(part).duration_s == pytest.approx(2.0, abs=0.1)
    r, _g, b = _frame_rgb(second)
    assert b > 200 and r < 60  # starts on the first blue frame, not the red keyframe
    r, _g, b = _frame_rgb(first, last=True)
    assert r > 200 and b < 60  # ends before the blue
    assert progress and progress[-1] <= 1.0
    assert not list(tmp_path.glob(".*.part"))


@has_ffmpeg
def test_cut_failure_leaves_nothing(red_blue_clip, tmp_path):
    with pytest.raises(media_format.MediaError):
        media_format.cut(red_blue_clip, tmp_path / "x.mp4", 3, 3)
    with pytest.raises(media_format.MediaError):
        media_format.cut(tmp_path / "missing.mp4", tmp_path / "x.mp4", 0, 1)
    assert list(tmp_path.iterdir()) == []
