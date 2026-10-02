"""Value types for the watch timer (WT-*)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo


class CountingMode(StrEnum):
    IGNORE_PAUSES = "ignore_pauses"
    WALL_CLOCK = "wall_clock"


class Activity(StrEnum):
    PLAYING = "playing"  # the caller maps BUFFERING to PLAYING
    PAUSED = "paused"
    STOPPED = "stopped"


class TimeUpReason(StrEnum):
    ALLOWANCE = "allowance"
    SESSION_MAX = "session_max"
    BLOCKED = "blocked"


class Action(StrEnum):
    CONTINUE = "continue"
    FINISH_THEN_STOP = "finish_then_stop"  # let the current episode end, no autoplay
    STOP_NOW = "stop_now"


@dataclass(frozen=True)
class TimerSettings:
    reset_time: time  # local wall time
    tz: ZoneInfo
    grace_cap_s: float = 900
    session_break_s: float = 900


@dataclass(frozen=True)
class ProfilePolicy:
    profile_id: int
    allowance_s: float | None
    mode: CountingMode
    max_session_s: float | None


@dataclass
class DayUsage:
    profile_id: int
    day: date
    used_s: float = 0.0
    extra_s: float = 0.0
    unlimited: bool = False
    blocked: bool = False

    def remaining_s(self, allowance_s: float | None) -> float | None:
        if self.unlimited or allowance_s is None:
            return None
        return max(0.0, allowance_s + self.extra_s - self.used_s)


@dataclass(frozen=True)
class ProfileStatus:
    """One profile on its own, whether or not it is watching (PR-2, PR-3)."""

    profile_id: int
    remaining_s: float | None  # None = unlimited today
    can_start: bool  # could this profile start a pick on its own now?
    reason: TimeUpReason | None  # why not, when it can't
    session_elapsed_s: float | None  # wall-clock length of this profile's viewing session (WT-3)
    watching: bool  # one of the current watchers


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: TimeUpReason | None  # set when exhausted/blocked
    grace_deadline: datetime | None
    remaining_s: float | None  # min across watching profiles; None = unlimited
    can_start: bool  # may a new pick start now?
    autoplay_allowed: bool  # may the next episode auto-start after this one?
    session_started_at: datetime | None
    session_elapsed_s: float | None  # wall-clock length of the current viewing session
