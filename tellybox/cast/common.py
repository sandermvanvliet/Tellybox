"""Types shared by the cast controller and its device-session mixin (kept apart to avoid an import cycle)."""

from __future__ import annotations

from enum import StrEnum

from tellybox.timer import Decision

RESUME_TAIL_S = 10.0         # don't resume within the last seconds of an episode


class EndReason(StrEnum):
    FINISHED = "finished"
    REPLACED = "replaced"          # a new pick replaced it (A-5)
    STOPPED = "stopped"            # stop from the kid app or another controller
    PARENT_STOP = "parent_stop"    # WT-7 stop now
    TIME_UP = "time_up"            # WT-4/WT-5/WT-3
    BLOCKED = "blocked"            # WT-7 block
    TAKEN_OVER = "taken_over"      # another app or cast took the device (WT-9, PB-5)
    DISCONNECTED = "disconnected"  # PB-5
    RESTART = "restart"            # our process restarted and the media was gone (NF-7)
    LOAD_FAILED = "load_failed"


class PlayRefused(Exception):
    def __init__(self, decision: Decision) -> None:
        super().__init__(f"play refused: {decision.reason}")
        self.decision = decision


class UnknownProfile(ValueError):
    """A pick named a profile that does not exist (PR-2)."""


class ShowNotAllowed(Exception):
    """A pick named an episode whose show the group may not see (PR-7)."""
