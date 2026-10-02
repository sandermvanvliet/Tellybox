"""Constants and helpers for the watch-timer tests (WT-*).

All tests run in Europe/Amsterdam with the default 04:00 reset. The default
start, 2026-09-28 12:00 UTC, is 14:00 local (CEST, UTC+2).
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tellybox.timer import CountingMode, ProfilePolicy

AMS = ZoneInfo("Europe/Amsterdam")
START = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
MIN = 60.0


def policy(
    profile_id: int = 1,
    allowance_min: float | None = 60,
    mode: CountingMode = CountingMode.IGNORE_PAUSES,
    max_session_min: float | None = 90,
) -> ProfilePolicy:
    return ProfilePolicy(profile_id, allowance_min * MIN if allowance_min is not None else None, mode, max_session_min * MIN if max_session_min is not None else None)
