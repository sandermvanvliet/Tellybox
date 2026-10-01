"""ES-4: find runs of sampled frames that look like one of the show's title cards (dHash distance)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from tellybox.detect import Hit, Reference


def hits(
    frames: Iterable[tuple[float, np.ndarray]], references: Sequence[Reference], threshold: int,
    *, max_gap_s: float = 1.5,
) -> list[Hit]:
    """Runs of frames within ``threshold`` of any reference (gaps up to ``max_gap_s`` bridged). Slice A."""
    raise NotImplementedError
