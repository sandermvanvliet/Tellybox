"""ES-4: decode a video at about 2 fps into small gray frames through an ffmpeg pipe."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np

from tellybox.detect import SAMPLE_FILTER, SAMPLE_FPS, Progress


def frames(
    video: Path, *, fps: float = SAMPLE_FPS, start_s: float = 0.0, end_s: float | None = None,
    on_progress: Progress | None = None, nice: int = 10,
) -> Iterator[tuple[float, np.ndarray]]:
    """(time in the video, normalised gray frame) at ``fps`` within [start_s, end_s).

    One ffmpeg process: ``fps=<fps>,`` + SAMPLE_FILTER, raw gray frames on stdout. Implemented in slice A.
    """
    raise NotImplementedError
