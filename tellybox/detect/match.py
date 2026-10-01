"""ES-4: find runs of sampled frames that look like one of the show's title cards (dHash distance)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from tellybox.detect import Hit, Reference, hamming, hash_region


def hits(
    frames: Iterable[tuple[float, np.ndarray]], references: Sequence[Reference], threshold: int,
    *, max_gap_s: float = 1.5,
) -> list[Hit]:
    """Runs of frames within ``threshold`` of any reference (gaps up to ``max_gap_s`` bridged)."""
    hashes = [r.hash() for r in references]
    out: list[Hit] = []
    cur: list | None = None  # [start, end, best distance, reference index]
    for t, frame in frames:
        best, best_i = None, 0
        for i, ref in enumerate(references):
            d = hamming(hash_region(frame, ref.region), hashes[i])
            if best is None or d < best:
                best, best_i = d, i
        if best is not None and best <= threshold:
            if cur is not None and t - cur[1] <= max_gap_s:
                cur[1] = t
                if best < cur[2]:
                    cur[2], cur[3] = best, best_i
            else:
                if cur is not None:
                    out.append(Hit(*cur))
                cur = [t, t, best, best_i]
    if cur is not None:
        out.append(Hit(*cur))
    return out
