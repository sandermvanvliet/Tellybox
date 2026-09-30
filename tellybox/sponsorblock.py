"""SponsorBlock (v3, SB-1..SB-5): categories, segments and position remapping.

Segments are cut out of the file at download by yt-dlp (`--sponsorblock-remove`); this
module only decides which categories apply and works with the segment lists yt-dlp
reports. All times are on the original (uncut) YouTube timeline unless named otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

from tellybox.i18n import N_

# yt-dlp's category keys (SponsorBlockPP.CATEGORIES, minus the non-skippable ones), in the
# order shown in the admin. Labels are translated where they're shown.
CATEGORIES: dict[str, str] = {
    "sponsor": N_("Sponsor"),
    "selfpromo": N_("Unpaid or self promotion"),
    "interaction": N_("Interaction reminder (subscribe)"),
    "intro": N_("Intermission or intro animation"),
    "outro": N_("End cards or credits"),
    "preview": N_("Preview or recap"),
    "hook": N_("Hook or greetings"),
    "filler": N_("Filler tangent"),
    "music_offtopic": N_("Non-music section"),
}
DEFAULT: tuple[str, ...] = ("sponsor", "selfpromo", "interaction")  # A-10
RECHECK_DAYS = 7  # SB-3
SAME_TOLERANCE_S = 0.5  # segments differing by less than this are the same (SponsorBlock votes jitter)

SB_STATUSES = ("cut", "none", "unreachable", "off", "admin_off")


@dataclass(frozen=True)
class Segment:
    category: str
    start_s: float
    end_s: float

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s


def parse_csv(value: str | None) -> list[str]:
    """Known categories from a stored CSV, in CATEGORIES order. None and '' give []."""
    wanted = {c.strip() for c in (value or "").split(",")}
    return [c for c in CATEGORIES if c in wanted]


def to_csv(categories: Iterable[str]) -> str:
    """Stored form: known categories only, CATEGORIES order, '' when none."""
    return ",".join(parse_csv(",".join(categories)))


def effective_categories(conn: sqlite3.Connection, show_id: int | None) -> list[str]:
    """SB-2: the show's own setting, else the global one. [] = SponsorBlock off.

    show_id None (not yet published, no show found) uses the global setting.
    """
    if show_id is not None:
        row = conn.execute("SELECT sponsorblock_categories FROM show WHERE id = ?", (show_id,)).fetchone()
        if row is not None and row["sponsorblock_categories"] is not None:
            return parse_csv(row["sponsorblock_categories"])
    row = conn.execute("SELECT sponsorblock_categories FROM settings WHERE id = 1").fetchone()
    return parse_csv(row["sponsorblock_categories"]) if row else list(DEFAULT)


def segments_from_info(chapters: Sequence[dict] | None, categories: Iterable[str]) -> list[Segment]:
    """The segments to remove, from yt-dlp's `sponsorblock_chapters` (SB-1).

    Only `skip` segments in `categories`; sorted and with overlaps merged (the merged
    segment keeps the first one's category), as yt-dlp's ModifyChapters cuts them.
    """
    wanted = set(categories)
    raw = sorted(
        (
            Segment(c["category"], float(c["start_time"]), float(c["end_time"]))
            for c in chapters or []
            if c.get("category") in wanted and c.get("type", "skip") == "skip"
            and float(c["end_time"]) > float(c["start_time"])
        ),
        key=lambda s: (s.start_s, s.end_s),
    )
    return merge(raw)


def merge(segments: Iterable[Segment]) -> list[Segment]:
    """Sort and merge overlapping or touching segments."""
    out: list[Segment] = []
    for s in sorted(segments, key=lambda s: (s.start_s, s.end_s)):
        if out and s.start_s <= out[-1].end_s:
            if s.end_s > out[-1].end_s:
                out[-1] = Segment(out[-1].category, out[-1].start_s, s.end_s)
        else:
            out.append(s)
    return out


def removed_s(segments: Iterable[Segment]) -> float:
    return sum(s.length_s for s in segments)


def same_segments(a: Sequence[Segment], b: Sequence[Segment], tol: float = SAME_TOLERANCE_S) -> bool:
    """SB-3: whether a re-check found nothing new. Categories are ignored; only the cuts matter."""
    a, b = merge(a), merge(b)
    return len(a) == len(b) and all(
        abs(x.start_s - y.start_s) < tol and abs(x.end_s - y.end_s) < tol for x, y in zip(a, b)
    )


def to_original(t: float, cuts: Sequence[Segment]) -> float:
    """Time in a file with `cuts` removed → time on the original timeline."""
    for c in merge(cuts):
        if c.start_s <= t:
            t += c.length_s
        else:
            break
    return t


def from_original(t: float, cuts: Sequence[Segment]) -> float:
    """Original time → time in a file with `cuts` removed. Inside a cut → where the cut was."""
    shift = 0.0
    for c in merge(cuts):
        if t >= c.end_s:
            shift += c.length_s
        elif t > c.start_s:
            return c.start_s - shift
        else:
            break
    return t - shift


def remap_position(position_s: float, old_cuts: Sequence[Segment], new_cuts: Sequence[Segment]) -> float:
    """SB-3: a saved position in the replaced file → the same moment in the new file."""
    return max(0.0, from_original(to_original(position_s, old_cuts), new_cuts))


def dumps(segments: Iterable[Segment]) -> str:
    return json.dumps([asdict(s) for s in segments])


def loads(value: str | None) -> list[Segment]:
    return [Segment(d["category"], float(d["start_s"]), float(d["end_s"])) for d in json.loads(value or "[]")]
