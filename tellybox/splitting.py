"""Episode splitting (v5, ES-1, ES-2, ES-7, ES-8): the cut plan of one source video.

A plan is a list of contiguous segments covering the source file from 0 to its duration.
Each kept segment becomes one episode; a dropped one (``keep=False``, e.g. a channel intro)
is left out. All times are on the timeline of the file on disk, which is after any
SponsorBlock cuts (SB-1). This module is pure: storage lives in ``library``, cutting in
``media_format.cut`` and the job in ``ingest``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

from tellybox import sponsorblock
from tellybox.i18n import _

MIN_KEPT_S = 5.0  # a kept part shorter than this is almost certainly a misplaced cut
MIN_DROPPED_S = 0.1
MAX_TITLE = 200
MAX_SEGMENTS = 200
END_TOLERANCE_S = 1.0  # the plan's end may differ this much from the probed duration
_EPS = 1e-3

STATUSES = ("draft", "review", "approved", "cutting", "done", "failed")
ORIGINS = ("chapters", "manual", "detected")
EDITABLE = ("draft", "review", "failed", "done")  # approved and cutting wait for the split job


class SplitInvalid(ValueError):
    """The plan can't be cut; the message is translated and shown to the admin."""


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float
    title: str = ""
    keep: bool = True

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s


def _t(value: float) -> float:
    return round(float(value), 3)


def whole(duration_s: float, title: str = "") -> list[Segment]:
    """The plan of an unsplit video: one kept segment."""
    return [Segment(0.0, _t(duration_s), title)]


def segments_from_cuts(duration_s: float, cuts: Iterable[float], titles: Sequence[str] = ()) -> list[Segment]:
    """Cut points (seconds) → contiguous kept segments. Points outside (0, duration) and duplicates are ignored.

    ``titles[i]`` names segment i when given.
    """
    points = sorted({_t(c) for c in cuts if _EPS < c < duration_s - _EPS})
    bounds = [0.0, *points, _t(duration_s)]
    return [
        Segment(a, b, titles[i] if i < len(titles) else "")
        for i, (a, b) in enumerate(zip(bounds, bounds[1:]))
    ]


def cuts_of(segments: Sequence[Segment]) -> list[float]:
    """The cut points between segments (every start except the first)."""
    return [s.start_s for s in segments[1:]]


def segments_from_chapters(chapters: Sequence[dict], duration_s: float) -> list[Segment]:
    """ES-1: one segment per chapter, titled after it. Chapters are ``{"start_s", "end_s", "title"}``.

    Only the chapter starts are used, so gaps or overlaps between chapters don't matter.
    """
    titles = {_t(c["start_s"]): (c.get("title") or "").strip() for c in chapters}
    segments = segments_from_cuts(duration_s, titles)
    # A segment is named after the chapter that starts it; the first one may start before any chapter.
    return [Segment(s.start_s, s.end_s, titles.get(s.start_s, "")) for s in segments]


def chapters_on_file(
    chapters: Sequence[dict] | None, duration_s: float | None, sb_segments: Sequence[sponsorblock.Segment]
) -> list[dict]:
    """The chapters on the file's timeline.

    Since step 12 the download's chapters (already shifted by yt-dlp for the SponsorBlock cuts)
    are stored. Rows written before that may hold the add-time chapters on the original timeline:
    when the file was cut and the chapters run past its end, they're mapped onto the file (SB-1).
    """
    if not chapters:
        return []
    out = [dict(c) for c in chapters]
    last_end = max(float(c.get("end_s") or c["start_s"]) for c in out)
    if sb_segments and duration_s and last_end > duration_s + END_TOLERANCE_S:
        out = [
            {**c, "start_s": _t(sponsorblock.from_original(float(c["start_s"]), sb_segments)),
             "end_s": _t(sponsorblock.from_original(float(c.get("end_s") or c["start_s"]), sb_segments))}
            for c in out
        ]
    return sorted(out, key=lambda c: float(c["start_s"]))


def check_shape(segments: Sequence[Segment], duration_s: float) -> None:
    """What a saved draft must satisfy: contiguous parts from the start to the end of the video."""
    if not segments:
        raise SplitInvalid(_("The plan has no parts."))
    if len(segments) > MAX_SEGMENTS:
        raise SplitInvalid(_("A video can be split into at most %(n)d parts.") % {"n": MAX_SEGMENTS})
    if abs(segments[0].start_s) > _EPS:
        raise SplitInvalid(_("The first part must start at the beginning of the video."))
    if abs(segments[-1].end_s - duration_s) > END_TOLERANCE_S:
        raise SplitInvalid(_("The last part must end at the end of the video."))
    for prev, seg in zip(segments, segments[1:]):
        if abs(seg.start_s - prev.end_s) > _EPS:
            raise SplitInvalid(_("Parts must follow each other without gaps."))
    for seg in segments:
        if seg.length_s <= 0:
            raise SplitInvalid(_("Parts must follow each other without gaps."))
        if len(seg.title) > MAX_TITLE:
            raise SplitInvalid(_("Titles can be at most %(n)d characters.") % {"n": MAX_TITLE})


def validate(segments: Sequence[Segment], duration_s: float) -> None:
    """Raise SplitInvalid unless the plan can be cut into episodes (ES-8)."""
    check_shape(segments, duration_s)
    for seg in segments:
        if seg.length_s < (MIN_KEPT_S if seg.keep else MIN_DROPPED_S):
            raise SplitInvalid(_("Each part must be at least %(n)d seconds long.") % {"n": int(MIN_KEPT_S)})
    kept = [s for s in segments if s.keep]
    if not kept:
        raise SplitInvalid(_("Keep at least one part."))
    if len(segments) == 1:
        raise SplitInvalid(_("Add a cut, or leave out a part, before approving."))


def kept_titles(segments: Sequence[Segment], fallback: str) -> list[str]:
    """Episode titles for the kept segments: the segment's own, else ``"<fallback> (n)"`` (1-based)."""
    kept = [s for s in segments if s.keep]
    return [s.title.strip() or f"{fallback} ({i})" for i, s in enumerate(kept, 1)]


def episode_file_name(youtube_id: str, job_id: int, n: int) -> str:
    """File name of the n-th (1-based) kept part, next to the source in ``shows/<show>/``.

    The split job's id keeps a re-split from overwriting the parts it replaces before it commits.
    """
    return f"{youtube_id}-s{job_id}-{n:02d}.mp4"


def dumps(segments: Iterable[Segment]) -> str:
    return json.dumps([asdict(s) for s in segments])


def loads(value: str | None) -> list[Segment]:
    return [from_dict(d) for d in json.loads(value or "[]")]


def from_dict(d: dict) -> Segment:
    """One segment from JSON (the admin API, ``segments_json``). Raises SplitInvalid on bad input."""
    try:
        return Segment(_t(d["start_s"]), _t(d["end_s"]), str(d.get("title") or "").strip(), bool(d.get("keep", True)))
    except (KeyError, TypeError, ValueError) as exc:
        raise SplitInvalid(_("The plan is malformed.")) from exc
