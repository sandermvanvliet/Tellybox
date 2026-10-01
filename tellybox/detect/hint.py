"""ES-5: use the approximate episode length to drop false hits and look harder for missed cards."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from tellybox.detect import Hit

TOO_CLOSE = 0.5  # hits closer than this times the hint to the previous kept one are dropped
TOO_FAR = 1.5  # a gap longer than this times the hint is re-scanned around the expected boundary
RESCAN_SPAN = 0.2  # ± this times the hint around the expected boundary


def apply(
    found: Sequence[Hit], duration_s: float, length_hint_s: float | None,
    rescan: Callable[[float, float], list[Hit]],
) -> list[Hit]:
    """Filter and fill ``found`` using the hint. ``rescan(start_s, end_s)`` samples that span again
    (denser, looser threshold) and returns its hits. Without a hint, ``found`` is returned sorted."""
    ordered = sorted(found, key=lambda h: h.start_s)
    if not length_hint_s or length_hint_s <= 0:
        return ordered
    hint = length_hint_s

    kept: list[Hit] = []
    for h in ordered:
        if kept and h.start_s - kept[-1].start_s < TOO_CLOSE * hint:
            if h.distance < kept[-1].distance:
                kept[-1] = h
            continue
        kept.append(h)

    result = list(kept)
    # Gaps: from the start of the video to the first hit, between hits, and from the last hit to the end.
    bounds = [0.0] + [h.start_s for h in kept] + [duration_s]
    for prev, nxt in zip(bounds, bounds[1:]):
        if nxt - prev <= TOO_FAR * hint:
            continue
        cur = prev
        while True:
            expected = cur + hint
            if expected >= nxt - TOO_CLOSE * hint:
                break
            lo, hi = max(0.0, expected - RESCAN_SPAN * hint), min(duration_s, expected + RESCAN_SPAN * hint)
            best = min(rescan(lo, hi), key=lambda h: h.distance, default=None)
            if best is not None:
                result.append(replace(best, rescanned=True))
                cur = best.start_s
            else:
                cur = expected
    return sorted(result, key=lambda h: h.start_s)
