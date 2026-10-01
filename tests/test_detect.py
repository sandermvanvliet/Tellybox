"""Smart splitting (v6) detection engine: sampling, matching, hint, snapping, OCR, full detect (ES-3..ES-9, NF-6)."""

from __future__ import annotations

import shutil
import time

import pytest

from tellybox import detect
from tellybox.media_format import MediaError
from tellybox.detect import Hit, Reference, Region, hint, match, ocr, sample, snap
from tests.detect_clips import LOGO_REGION, font_file, make_compilation

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)

REGION = Region(*LOGO_REGION)
EPISODES = [12, 15, 10, 14]


@pytest.fixture(scope="session")
def comp(tmp_path_factory):
    return make_compilation(tmp_path_factory.mktemp("detect") / "comp.mp4", EPISODES, weak={2})


@pytest.fixture(scope="session")
def ref(comp):
    return Reference(detect.reference_hash(comp.reference, REGION), REGION)


@pytest.fixture(scope="session")
def frames_all(comp):
    return list(sample.frames(comp.path))


def test_frames_count_and_times(comp, frames_all):
    assert abs(len(frames_all) - comp.duration_s * detect.SAMPLE_FPS) <= 2
    times = [t for t, _ in frames_all]
    assert times[0] == 0 and times[1] == pytest.approx(0.5)
    assert frames_all[0][1].shape == (180, detect.SAMPLE_WIDTH)


def test_frames_span_and_progress(comp):
    seen: list[float] = []
    got = list(sample.frames(comp.path, fps=5, start_s=10, end_s=14, on_progress=seen.append))
    assert len(got) == 20
    assert got[0][0] == 10 and got[-1][0] == pytest.approx(13.8)
    assert seen[-1] == 1.0 and seen == sorted(seen)


def test_frames_stops_early_and_fails_on_bad_file(comp, tmp_path):
    it = sample.frames(comp.path)
    next(it)
    it.close()  # kills ffmpeg, must not hang
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope")
    with pytest.raises(MediaError):
        list(sample.frames(bad))


def test_hits_find_each_normal_card_and_nothing_else(comp, ref, frames_all):
    found = match.hits(frames_all, [ref], detect.DEFAULT_THRESHOLD)
    normal = [s for i, s in enumerate(comp.card_starts) if i != 2]
    assert len(found) == len(normal)
    for h, start in zip(found, normal):
        assert start - 0.5 <= h.start_s <= start + 0.5
        assert h.distance <= 1


def test_weak_card_is_missed_by_default_and_found_by_rescan(comp, ref, frames_all):
    found = match.hits(frames_all, [ref], detect.DEFAULT_THRESHOLD)
    weak_start = comp.card_starts[2]
    assert not any(abs(h.start_s - weak_start) < 1.5 for h in found)

    def rescan(lo, hi):
        return match.hits(sample.frames(comp.path, fps=5, start_s=lo, end_s=hi), [ref],
                          detect.DEFAULT_THRESHOLD + detect.RESCAN_LOOSER)

    out = hint.apply(found, comp.duration_s, 16.0, rescan)
    assert len(out) == len(comp.card_starts)
    rescued = [h for h in out if h.rescanned]
    assert len(rescued) == 1 and abs(rescued[0].start_s - weak_start) <= 0.5


def test_hint_drops_close_false_hit_keeps_better():
    hits = [Hit(100, 102, 1), Hit(110, 111, 5), Hit(300, 302, 0)]
    out = hint.apply(hits, 400, 120, lambda lo, hi: [])
    assert [h.start_s for h in out] == [100, 300]
    better = hint.apply([Hit(100, 102, 4), Hit(110, 111, 1)], 150, 120, lambda lo, hi: [])
    assert [h.start_s for h in better] == [110]


def test_hint_rescans_gaps_at_start_and_end_and_without_hint_sorts():
    calls = []

    def rescan(lo, hi):
        calls.append((lo, hi))
        return []

    hint.apply([Hit(250, 252, 0)], 520, 100, rescan)
    # start gap: an expected boundary near 100 (200 is too close to the hit at 250); end gap: 350 and 450
    assert [round((lo + hi) / 2) for lo, hi in calls] == [100, 350, 450]
    unsorted = [Hit(9, 10, 0), Hit(1, 2, 0)]
    assert [h.start_s for h in hint.apply(unsorted, 20, None, rescan)] == [1, 9]


def test_snap_lands_on_black_start(comp, ref):
    for card, black in zip(comp.card_starts, comp.black_starts):
        at, kind = snap.snap(comp.path, card + 0.3, 30)
        assert kind == "black" and abs(at - black) <= 0.1


def test_snap_without_change_returns_the_hit(comp):
    # inside the first episode's content: steady source, no black, no scene change
    t = comp.card_starts[0] + 2.0 + 8.0
    at, kind = snap.snap(comp.path, t, 3)
    assert (at, kind) == (t, None)


def test_detect_full(comp, ref):
    progress: list[float] = []
    profile = detect.Profile([ref], length_hint_s=16.0)
    cuts = detect.detect(comp.path, profile, on_progress=progress.append)
    assert len(cuts) == len(comp.black_starts)
    for cut, black, card in zip(cuts, comp.black_starts, comp.card_starts):
        assert abs(cut.at_s - black) <= 0.2 and cut.snapped == "black"
        assert abs(cut.title_hit_s - card) <= 0.5
        assert 0 < cut.confidence <= 1
    assert [c.at_s for c in cuts] == sorted(c.at_s for c in cuts)
    assert cuts[0].confidence > 0.9 and cuts[2].confidence < 0.5  # the weak card came from the re-scan
    assert cuts[2].extra["rescanned"] is True
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert 0.79 < max(p for p in progress if p < 0.81) <= 0.8


def test_detect_without_references_or_cards(comp, ref):
    assert detect.detect(comp.path, detect.Profile([])) == []
    # a reference hashed from a card region that never appears in a 3 s slice of plain content
    other = Reference("a5" * 8, Region(0.0, 0.0, 1.0, 1.0))
    assert detect.detect(comp.path, detect.Profile([other])) == []


def test_detect_never_snaps_past_the_previous_card(comp, ref, monkeypatch):
    windows = []

    def fake_snap(video, hit_s, window_s):
        windows.append((hit_s, window_s))
        return hit_s, None

    monkeypatch.setattr(snap, "snap", fake_snap)
    detect.detect(comp.path, detect.Profile([ref], snap_window_s=60))
    assert windows[0][1] == 60  # the first card can look back the whole window
    for (prev_hit, _), (hit, window) in zip(windows, windows[1:]):
        assert window < 60 and hit - window >= prev_hit  # limited to after the previous card


@pytest.mark.skipif(not ocr.available() or font_file() is None, reason="needs tesseract and a system font")
def test_ocr_reads_episode_title(comp, ref):
    cuts = detect.detect(comp.path, detect.Profile([ref], ocr=True, ocr_region=Region(0.2, 0.55, 0.6, 0.2),
                                                   length_hint_s=16.0))
    assert cuts[1].title == "Episode 2"


@pytest.mark.skipif(not ocr.available() or font_file() is None, reason="needs tesseract and a system font")
def test_detect_reads_titles_from_the_whole_frame_by_default(comp, ref):
    cuts = detect.detect(comp.path, detect.Profile([ref], ocr=True))  # ref's region is the logo, not the text
    assert [c.title for c in cuts if c.title] and all(c.title.startswith("Episode") for c in cuts if c.title)


def test_ocr_unavailable_returns_empty(monkeypatch):
    import numpy as np

    monkeypatch.setattr(ocr, "available", lambda: False)
    assert ocr.read_title(np.zeros((90, 160), dtype=np.uint8), None) == ""


@pytest.mark.slow
def test_benchmark_thirty_minute_compilation(tmp_path):
    """NF-6: detection on a 30-minute 720p compilation takes under half its duration."""
    import subprocess

    small = make_compilation(tmp_path / "small.mp4", [20, 20])
    ref = Reference(detect.reference_hash(small.reference, REGION), REGION)
    # Build the long file by looping the 720p-scaled small one is cheap: encode once, concat many times.
    unit = tmp_path / "unit.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(small.path),
                    "-vf", "scale=1280:720", "-c:v", "libx264", "-preset", "veryfast", "-g", "50",
                    "-pix_fmt", "yuv420p", "-an", str(unit)], check=True)
    n = int(1800 // small.duration_s) + 1
    listing = tmp_path / "list.txt"
    listing.write_text("".join(f"file '{unit}'\n" for _ in range(n)))
    long = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", "-t", "1800", str(long)], check=True)
    start = time.monotonic()
    cuts = detect.detect(long, detect.Profile([ref], length_hint_s=small.duration_s / 2))
    elapsed = time.monotonic() - start
    print(f"NF-6 benchmark: {elapsed:.1f} s for 1800 s, {len(cuts)} cuts")
    assert len(cuts) >= 2 * (n - 1)
    assert elapsed < 900
