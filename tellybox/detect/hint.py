"""ES-5: use the approximate episode length to drop false hits and look harder for missed cards."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from tellybox.detect import Hit

TOO_CLOSE = 0.5  # hits closer than this times the hint to the previous kept one are dropped
TOO_FAR = 1.5  # a gap longer than this times the hint is re-scanned around the expected boundary
RESCAN_SPAN = 0.2  # ± this times the hint around the expected boundary


def apply(
    found: Sequence[Hit], duration_s: float, length_hint_s: float | None,
    rescan: Callable[[float, float], list[Hit]],
) -> list[Hit]:
    """Filter and fill ``found`` using the hint. ``rescan(start_s, end_s)`` samples that span again
    (denser, looser threshold) and returns its hits. Without a hint, ``found`` is returned as is. Slice A."""
    raise NotImplementedError
