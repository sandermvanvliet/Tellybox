"""One code path for parent overrides (WT-7, HA-4, HA-8): the admin dashboard's buttons and the JSON API
both come through `apply_override`, so they can't drift apart. Everything goes to the cast service, which
owns the timer.
"""

from __future__ import annotations

KINDS = ("extra_minutes", "unlimited", "block", "stop_now", "clear")
EXTRA_MINUTES_MAX = 240  # A-17
PROFILE_IDS_MAX = 20


def validate_profile_ids(profile_ids: list[int] | None) -> list[int] | None:
    """None means every profile; otherwise 1..20 distinct ids. Whether they exist is the cast service's call."""
    if profile_ids is None:
        return None
    if not 1 <= len(profile_ids) <= PROFILE_IDS_MAX:
        raise ValueError(f"profile_ids needs 1 to {PROFILE_IDS_MAX} ids")
    if any(p < 1 for p in profile_ids):
        raise ValueError("profile_ids must be positive")
    if len(set(profile_ids)) != len(profile_ids):
        raise ValueError("profile_ids must be distinct")
    return list(profile_ids)


async def apply_override(
    cast, kind: str, value: int | None = None, profile_ids: list[int] | None = None, source: str | None = None
) -> dict:
    """Apply an override through the cast service and return its state.

    `source` is the API token's name (None = the admin pages). Raises ValueError for a refused override
    (bad kind, minutes or profile ids) and lets CastUnavailable through; nothing was applied in either case.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown override {kind!r}")
    if kind == "extra_minutes":
        if not value or value <= 0:
            raise ValueError("extra_minutes needs a positive value")  # the cast service's text, translated (dashboard)
        if value > EXTRA_MINUTES_MAX:
            raise ValueError(f"extra_minutes is at most {EXTRA_MINUTES_MAX}")
    profile_ids = validate_profile_ids(profile_ids)
    return await cast.override(kind, value, profile_ids, source)
